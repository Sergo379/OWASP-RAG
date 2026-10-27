"""One serial RAG worker, persistent bounded conversations, and cancellable jobs."""

import logging
import queue
import re
import threading
import time
import uuid

import psutil
from filelock import FileLock, Timeout

from src.core.config import AppError
from src.core.service import BusyError
from src.generation.config import ChatRequest, load_generation
from src.generation.context import (
    adequacy,
    build_context,
    construct_prompt,
    history_messages,
    public_sources,
    validate_answer,
)
from src.generation.ollama import Cancellation, Cancelled, GenerationError, Ollama
from src.generation.resources import ResourceMonitor
from src.observability.tracing import Tracing
from src.preprocessing.tokenization import E5Tokenizer
from src.retrieval.pipeline import execute_search
from src.storage.files import now, read_json, write_json

log = logging.getLogger("owasp.rag")
TERMINAL = {"completed", "refused", "failed", "cancelled", "interrupted"}


def identifier(value):
    if not re.fullmatch(r"[0-9a-f]{32}", value):
        raise AppError("Некорректный идентификатор")
    return value


class Chat:
    def __init__(self, service, *, ollama=None, tracing=None, tokenizer=None):
        self.service = service
        self.config, self.prompts = load_generation(service.settings.root)
        self.ollama = ollama or Ollama(self.config)
        self.tracing = tracing or Tracing(service.settings.root)
        self.tokenizer = tokenizer
        self.root = service.store.root / "chat"
        self.jobs, self.guard = {}, threading.RLock()

    def prepare_tokenizer(self):
        if self.tokenizer is None:
            try:
                self.tokenizer = E5Tokenizer(self.config.tokenizer, self.service.store.root / "tokenizers")
            except AppError as exc:
                raise AppError(
                    "Токенизатор Qwen недоступен. Для первой подготовки нужна сеть: pixi run llm-check --prepare-tokenizer"
                ) from exc
        return self.tokenizer

    def session_path(self, session_id):
        return self.root / "sessions" / (identifier(session_id) + ".json")

    def job_path(self, request_id):
        return self.root / "requests" / (identifier(request_id) + ".json")

    def create_session(self):
        session = {"session_id": uuid.uuid4().hex, "created_at": now(), "messages": []}
        write_json(self.session_path(session["session_id"]), session)
        return session

    def session(self, session_id):
        session = read_json(self.session_path(session_id), None)
        if session is None:
            raise AppError("Диалог не найден; создайте новый")
        if session["messages"] and session["messages"][-1].get("status") == "running":
            self.get(session["messages"][-1]["request_id"])
            session = read_json(self.session_path(session_id))
        return session

    def clear(self, session_id):
        lock = FileLock(self.service.lock_path, timeout=0)
        try:
            with lock:
                session = self.session(session_id)
                session["messages"] = []
                write_json(self.session_path(session_id), session)
                return session
        except Timeout as exc:
            raise BusyError("Сначала остановите генерацию и дождитесь завершения операции") from exc

    def get(self, request_id):
        result = read_json(self.job_path(request_id), None)
        if result is None:
            raise AppError("Запрос не найден")
        if result["status"] == "running":
            try:
                with FileLock(self.service.lock_path, timeout=0):
                    result.update(
                        status="interrupted",
                        error="Процесс остановился; частичный ответ не завершён",
                        finished_at=now(),
                    )
                    write_json(self.job_path(request_id), result)
                    session = read_json(self.session_path(result["session_id"]))
                    for message in session["messages"]:
                        if message.get("request_id") == request_id and message["role"] == "assistant":
                            message.update(
                                status="interrupted", error=result["error"], content=result.get("answer", "")
                            )
                    write_json(self.session_path(result["session_id"]), session)
            except Timeout:
                pass
        result["trace_url"] = self.tracing.trace_url(result["trace_id"]) if result.get("trace_id") else None
        return result

    def start(self, request):
        # Recover an abandoned last answer before taking this process's lock.
        self.session(request.session_id)
        # Acquire synchronously so two HTTP callers cannot both start heavy models.
        lock = FileLock(self.service.lock_path, timeout=0, thread_local=False)
        try:
            lock.acquire()
        except Timeout as exc:
            raise BusyError("CPU/память заняты. Дождитесь завершения поиска, генерации или сборки") from exc
        try:
            session = self.session(request.session_id)
            request_id = uuid.uuid4().hex
            result = {
                "request_id": request_id,
                "session_id": request.session_id,
                "status": "running",
                "started_at": now(),
                "finished_at": None,
                "answer": "",
                "sources": [],
                "error": None,
                "trace_id": None,
                "trace_url": None,
                "diagnostics": {},
            }
            job = {
                "result": result,
                "cancel": Cancellation(),
                "events": queue.Queue(maxsize=2048),
                "done": threading.Event(),
            }
            with self.guard:
                # Retain only live/recent stream queues; disk records remain available.
                for key in list(self.jobs):
                    if len(self.jobs) < 20:
                        break
                    if self.jobs[key]["result"]["status"] in TERMINAL:
                        del self.jobs[key]
                self.jobs[request_id] = job
            write_json(self.job_path(request_id), result)
            history = list(session["messages"])
            session["messages"].extend(
                [
                    {"role": "user", "content": request.retrieval.query, "request_id": request_id},
                    {"role": "assistant", "content": "", "status": "running", "request_id": request_id},
                ]
            )
            session["messages"] = session["messages"][-self.config.max_messages :]
            # Keep complete pairs even if a user changes max_messages to an odd number.
            if session["messages"] and session["messages"][0]["role"] != "user":
                session["messages"] = session["messages"][1:]
            write_json(self.session_path(request.session_id), session)
            op = {
                "operation_id": request_id,
                "kind": "rag",
                "status": "running",
                "started_at": now(),
                "finished_at": None,
                "completed": 0,
                "total": 1,
                "current": "Retrieval",
                "error": None,
            }
            write_json(self.service.operation_path, op)
            threading.Thread(target=self._run, args=(request, job, history, op, lock), daemon=True).start()
            return dict(result)
        except BaseException:
            lock.release()
            raise

    def emit(self, job, event, data):
        try:
            job["events"].put_nowait({"event": event, "data": data})
        except queue.Full:
            # Polling by request ID remains authoritative if a slow client loses deltas.
            pass

    def cancel(self, request_id):
        with self.guard:
            job = self.jobs.get(identifier(request_id))
        if not job:
            return self.get(request_id)
        if job["result"]["status"] == "running":
            job["cancel"].cancel()
        return {"request_id": request_id, "cancellation_requested": True, "status": job["result"]["status"]}

    def _run(self, request, job, history, op, lock):
        result, cancel, started = job["result"], job["cancel"], time.perf_counter()
        trace = None
        rss_peak = psutil.Process().memory_info().rss
        partial, last_checkpoint = "", started
        monitor = ResourceMonitor()
        monitor.start()
        generation_config = self.config.model_copy(
            update={
                "num_predict": self.config.model_num_predict.get(request.llm, self.config.num_predict),
                "temperature": self.config.model_temperatures.get(request.llm, self.config.temperature),
            }
        )
        try:
            trace = self.tracing.request(
                result["request_id"],
                {
                    "llm": request.llm,
                    "index_id": request.retrieval.index_id,
                    "embedding_model": request.retrieval.model,
                    "prompt_mode": request.prompt_mode,
                    "prompt_version": self.prompts["version"],
                    "top_k": request.retrieval.top_k,
                    "threshold": request.retrieval.similarity_threshold,
                    "reranking": request.retrieval.reranking,
                    "candidate_pool_size": request.retrieval.candidate_pool_size,
                },
            )
            result["trace_id"] = trace.trace_id
            self.emit(job, "accepted", {"request_id": result["request_id"], "trace_id": trace.trace_id})
            with trace.step("INPUT_VALIDATION") as span:
                cancel.check()
                request = ChatRequest.model_validate(request.model_dump())
                span.update(
                    input=self.tracing.texts(query=request.retrieval.query),
                    metadata={"query_chars": len(request.retrieval.query), "session_id": request.session_id},
                )
            self.emit(job, "progress", {"stage": "Retrieval"})
            retrieval = execute_search(
                request.retrieval,
                self.service.vectors,
                self.service.models,
                self.service.retrieval_config,
                request_id=result["request_id"],
                observer=trace,
            )
            cancel.check()
            result["diagnostics"]["retrieval"] = {
                k: v for k, v in retrieval["diagnostics"].items() if k != "query"
            }
            if self.config.unload_retrieval:
                self.service.models.unload("embedding")
                self.service.models.unload("reranker")
            with trace.step("CONTEXT_BUILDING") as span:
                self.prepare_tokenizer()
                selected_history, history_info = history_messages(history, self.config, self.tokenizer)
                sources, messages, context_info = build_context(
                    retrieval["results"],
                    request.retrieval.query,
                    self.prompts[request.prompt_mode],
                    selected_history,
                    generation_config,
                    self.tokenizer,
                )
                gate = adequacy(request.retrieval.query, sources)
                span.update(
                    metadata={**context_info, **history_info, "adequacy_gate": gate},
                    input=self.tracing.texts(context="\n".join(s["original_text"] for s in sources)),
                )
                result["diagnostics"].update(
                    context=context_info,
                    history=history_info,
                    adequacy_gate=gate,
                    llm=request.llm,
                    llm_digest=self.config.models[request.llm],
                    prompt_mode=request.prompt_mode,
                    prompt_version=self.prompts["version"],
                    generation_config=generation_config.model_dump(exclude={"tokenizer"}),
                )
            with trace.step("PROMPT_CONSTRUCTION") as span:
                messages = construct_prompt(
                    sources, request.retrieval.query, self.prompts[request.prompt_mode], selected_history
                )
                span.update(
                    metadata={
                        "message_roles": [m["role"] for m in messages],
                        "estimated_prompt_tokens": context_info["estimated_prompt_tokens"],
                    },
                    input=self.tracing.texts(prompt=str(messages)),
                )
            if gate:
                with trace.step("OLLAMA_GENERATION", as_type="generation", model=request.llm) as span:
                    span.update(metadata={"status": "skipped", "reason": gate})
                answer, metrics = "INSUFFICIENT_CONTEXT", {"skipped": True}
            else:
                op["current"] = "Ollama · " + request.llm
                write_json(self.service.operation_path, op)
                self.emit(job, "progress", {"stage": "Ollama generation · provisional until validated"})

                def token(part):
                    nonlocal partial, last_checkpoint, rss_peak
                    cancel.check()
                    partial += part
                    rss_peak = max(rss_peak, psutil.Process().memory_info().rss)
                    self.emit(job, "token", {"text": part, "provisional": True})
                    if time.perf_counter() - last_checkpoint >= 1:
                        result["answer"] = partial
                        write_json(self.job_path(result["request_id"]), result)
                        last_checkpoint = time.perf_counter()

                with trace.step(
                    "OLLAMA_GENERATION",
                    as_type="generation",
                    model=request.llm,
                    model_parameters={
                        "temperature": generation_config.temperature,
                        "num_ctx": self.config.num_ctx,
                        "num_predict": generation_config.num_predict,
                        "think": request.llm in self.config.thinking_models,
                        "num_thread": self.config.num_thread,
                    },
                ) as span:
                    try:
                        answer, metrics = self.ollama.stream(request.llm, messages, cancel, token)
                    except GenerationError as exc:
                        result["diagnostics"]["generation"] = exc.metrics
                        span.update(
                            metadata=exc.metrics,
                            usage_details={
                                k: exc.metrics[source]
                                for k, source in (("input", "prompt_eval_count"), ("output", "eval_count"))
                                if isinstance(exc.metrics.get(source), int)
                            },
                        )
                        raise
                    span.update(
                        usage_details={
                            k: metrics[source]
                            for k, source in (("input", "prompt_eval_count"), ("output", "eval_count"))
                            if isinstance(metrics.get(source), int)
                        },
                        metadata=metrics,
                        output=self.tracing.texts(answer=answer),
                    )
            cancel.check()
            with trace.step("ANSWER_VALIDATION") as span:
                validation, final = validate_answer(answer, sources, request.retrieval.query)
                span.update(metadata=validation)
            result.update(
                status="completed" if validation["valid"] else "refused",
                answer=final,
                sources=public_sources(sources, validation["used_source_ids"]) if validation["valid"] else [],
            )
            result["diagnostics"].update(
                validation=validation,
                generation=metrics,
                grounding_limit="Citation IDs are checked; factual entailment still requires human review",
            )
            with trace.step("FINAL_ANSWER") as span:
                span.update(
                    metadata={"status": result["status"], "source_ids": validation["used_source_ids"]},
                    output=self.tracing.texts(answer=final),
                )
        except Cancelled as exc:
            result.update(status="cancelled", answer=partial, sources=[], error=str(exc))
        except AppError as exc:
            result.update(status="failed", answer=partial, sources=[], error=str(exc))
        except Exception as exc:
            result.update(
                status="failed", answer=partial, sources=[], error="Ошибка RAG; проверьте технические логи"
            )
            log.exception(
                "RAG failure",
                extra={
                    "operation": "rag",
                    "context": {"request_id": result["request_id"], "error_type": type(exc).__name__},
                },
            )
        finally:
            result.update(finished_at=now())
            result["diagnostics"].update(
                total_ms=round((time.perf_counter() - started) * 1000, 2), python_peak_rss_bytes=rss_peak
            )
            result["diagnostics"]["memory"] = monitor.finish()
            if trace:
                trace.close(result["status"])
                result["diagnostics"]["stages"] = trace.stages
                result["trace_url"] = self.tracing.trace_url(trace.trace_id)
            result["diagnostics"]["observability"] = self.tracing.status()
            try:
                write_json(self.job_path(result["request_id"]), result)
                session = read_json(self.session_path(request.session_id))
                for message in session["messages"]:
                    if message.get("request_id") == result["request_id"] and message["role"] == "assistant":
                        message.update(
                            content=result["answer"],
                            status=result["status"],
                            sources=result["sources"],
                            error=result["error"],
                            trace_id=result["trace_id"],
                        )
                write_json(self.session_path(request.session_id), session)
                op.update(
                    status="succeeded" if result["status"] in ("completed", "refused") else result["status"],
                    completed=1,
                    finished_at=now(),
                    current=result["status"],
                    error=result["error"],
                    result={
                        "request_id": result["request_id"],
                        "trace_id": result["trace_id"],
                        "status": result["status"],
                    },
                )
                history = read_json(self.service.store.root / "manifests/history.json", [])
                history.append(op)
                write_json(self.service.store.root / "manifests/history.json", history)
                write_json(self.service.operation_path, op)
            except (OSError, ValueError, TypeError):
                result.update(
                    status="failed",
                    sources=[],
                    error="Не удалось сохранить результат на диск; проверьте место и права доступа",
                )
            finally:
                lock.release()
                self.emit(job, "final", result)
                job["done"].set()

    def wait(self, request_id):
        with self.guard:
            job = self.jobs.get(request_id)
        if job:
            job["done"].wait()
            return dict(job["result"])
        while True:
            result = self.get(request_id)
            if result["status"] in TERMINAL:
                return result
            time.sleep(0.1)
