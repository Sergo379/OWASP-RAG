"""Serial inference with one durable checkpoint per measured question."""

import logging
import time

from filelock import FileLock, Timeout

from src.core.config import AppError
from src.generation.config import ChatRequest
from src.generation.resources import ResourceMonitor
from src.retrieval.models import SearchRequest
from src.retrieval.pipeline import execute_search
from src.storage.files import now
from src.storage.vectors import corpus_snapshot

from .experiments import protocol
from .indexes import Indexes, ensure_qwen_tokenizer
from .metrics import generation_scores, ranking, source_coverage
from .storage import RETRY, checked, identity, save

log = logging.getLogger("owasp.evaluation")


class StopRun(BaseException):
    pass


def search_request(parameters, question, index_id):
    return SearchRequest(
        query=question["question"],
        index_id=index_id,
        model=parameters["model"],
        top_k=parameters["k"],
        candidate_pool_size=parameters["pool"],
        similarity_threshold=parameters["threshold"],
        reranking=parameters["rerank"],
    )


def measure_retrieval(service, request, question):
    monitor = ResourceMonitor()
    with FileLock(service.lock_path, timeout=5):
        cpu = time.process_time()
        monitor.start()
        try:
            result = execute_search(request, service.vectors, service.models, service.retrieval_config)
        finally:
            memory = monitor.finish()
    diagnostics = result["diagnostics"]
    rows = result["results"]
    candidates = diagnostics["candidate_pool"]
    scores = ranking(rows, question["relevant_documents"], request.top_k)
    scores["gold_evidence_anchor_coverage"] = source_coverage(rows, question)
    scores["empty_context"] = float(not rows)
    return {
        "status": "completed",
        "request_id": result["request_id"],
        "retrieved": rows,
        "scores": scores,
        "candidates": candidates,
        "rank_changes": result["comparison"],
        "removed": diagnostics["removed"],
        "same_pool_without_rerank_scores": ranking(
            result["without_reranking"], question["relevant_documents"], request.top_k
        )
        if request.reranking
        else None,
        "same_pool_without_rerank": [
            {key: row[key] for key in ("document_id", "chunk_id", "retrieval_rank", "vector_similarity")}
            for row in result["without_reranking"]
        ]
        if request.reranking
        else None,
        "timings": diagnostics["timings"],
        "latency_ms": diagnostics["timings"]["total_ms"],
        "cpu_seconds": time.process_time() - cpu,
        **memory,
    }


def measure_generation(primary, service, request, question, config, stop):
    ensure_qwen_tokenizer(primary, service)
    chat = service.chat
    job = chat.start(
        ChatRequest(
            session_id=chat.create_session()["session_id"],
            retrieval=request,
            llm=config["llm"],
            prompt_mode=config["mode"],
            answer_format=config["answer_format"],
            retrieval_strategy=config["retrieval_strategy"],
        )
    )
    request_id = job["request_id"]
    done = chat.jobs[request_id]["done"]
    cpu = time.process_time()
    try:
        while not done.wait(0.25):
            if stop():
                chat.cancel(request_id)
    except KeyboardInterrupt:
        chat.cancel(request_id)
        chat.wait(request_id)
        raise
    result = chat.wait(request_id)
    diagnostics = result["diagnostics"]
    generation = diagnostics.get("generation", {})
    retrieval = diagnostics.get("retrieval", {})
    selected = [
        {**chunk, "document_id": group["document_id"], "title": group["title"], "url": group["url"]}
        for group in result.get("context_sources", [])
        for chunk in group["chunks"]
    ]
    selected.sort(key=lambda chunk: int(chunk["source_id"][1:]))
    error = result.get("error")
    status = result["status"]
    timeout_seconds = diagnostics.get("generation_config", {}).get("timeout_seconds")
    reached_deadline = timeout_seconds and diagnostics.get("total_ms", 0) >= (timeout_seconds - 3) * 1000
    if status == "failed" and error and reached_deadline and "timeout" in error.casefold():
        status = "timeout"
    scores = generation_scores(result, question)
    scores["selected_evidence_anchor_coverage"] = source_coverage(selected, question)
    return {
        "status": status,
        "chat_status": result["status"],
        "request_id": request_id,
        "error": error,
        "answer": result["answer"],
        "citations": result["sources"],
        "selected_context": selected,
        "context_fingerprint": identity(
            [(s["chunk_id"], s.get("prompt_text", s.get("text"))) for s in selected]
        ),
        "scores": scores,
        "validation": diagnostics.get("validation"),
        "adequacy": diagnostics.get("adequacy_gate"),
        "context": diagnostics.get("context"),
        "effective_generation_config": diagnostics.get("generation_config"),
        "generation": generation,
        "generation_attempts": diagnostics.get("generation_attempts", []),
        "trace_id": result["trace_id"],
        "observability": diagnostics.get("observability"),
        "model_digest": chat.config.models[config["llm"]],
        "latency_ms": diagnostics.get("total_ms"),
        "ttft_ms": generation.get("ttft_ms"),
        "eval_count": generation.get("eval_count"),
        "cpu_seconds": time.process_time() - cpu,
        "cpu_process_scope": "Python; Ollama external CPU not sampled",
        "retrieval": {
            key: retrieval.get(key) for key in ("candidate_pool", "selected_candidates", "removed", "timings")
        },
        **diagnostics.get("memory", {}),
    }


class Runner:
    def __init__(self, service, dataset, store):
        self.service, self.dataset, self.store = service, dataset, store
        self.indexes = Indexes(service, dataset)

    def run(self, run_id, retry=False, resume=False):
        try:
            with FileLock(self.store.lock_path, timeout=0):
                if resume:
                    self.store.clear_stop(run_id)
                return self.execute(run_id, retry)
        except Timeout as exc:
            if not resume and self.store.state(run_id).get("status") == "queued":
                self.store.update(run_id, status="skipped", error="Другой experiment run уже выполняется")
            raise AppError("Другой experiment run уже выполняется") from exc

    def execute(self, run_id, retry):
        manifest = self.store.manifest(run_id)
        protocol_now = protocol(self.service)
        if (
            manifest["dataset_sha256"] != self.dataset.manifest["sha256"]
            or manifest["source_snapshot"] != self.dataset.manifest["corpus_snapshot"]
            or manifest["environment"] != protocol_now
        ):
            raise AppError("Изменились dataset, runtime или pipeline. Resume требует исходной конфигурации")
        rows = self.store.audit(run_id)
        questions = {q["id"]: q for q in self.dataset.split(manifest["split"])}
        tasks = [(c, qid) for c in manifest["configurations"] for qid in c["questions"]]
        indexes = {}
        active_services = set()

        def stop():
            return self.store.stopped(run_id)

        def state(status, current=None, **extra):
            self.store.update(
                run_id, status=status, current=current, saved=len(rows), total=len(tasks), **extra
            )

        state("running")
        try:
            for conf, qid in tasks:
                if stop():
                    raise StopRun()
                label, parameters = conf["label"], conf["parameters"]
                key = f"{label}__{qid}"
                if key in rows and (not retry or rows[key]["status"] not in RETRY):
                    continue
                started = now()
                current = {"configuration": label, "question_id": qid}
                state("running", current)
                if corpus_snapshot(self.service.store.manifest()) != manifest["source_snapshot"]:
                    raise AppError("Корпус изменился во время experiment run")

                def progress(**fields):
                    if stop():
                        raise StopRun()
                    state("running", {**current, **fields})

                try:
                    if label not in indexes:
                        resolved, details = self.indexes.resolve(parameters, progress)
                        selected = manifest.get("selection", {}).get("development_index")
                        if selected and selected["id"] != details["index_id"]:
                            raise AppError("Финальный индекс отличается от выбранного в development")
                        index_path = self.store.path(run_id) / "indexes" / (label + ".json")
                        if index_path.exists():
                            previous = checked(index_path)
                            if (previous["index_id"], previous["published"]["generation"]) != (
                                details["index_id"],
                                details["published"]["generation"],
                            ):
                                raise AppError("Изменился index ID или generation. Создайте отдельный run")
                        else:
                            save(index_path, details)
                        indexes[label] = (resolved, details)
                    service, details = indexes[label]
                    active_services.add(service)
                    question = questions[qid]
                    request = search_request(parameters, question, details["index_id"])
                    if "llm" in parameters:
                        row = measure_generation(self.service, service, request, question, parameters, stop)
                    else:
                        row = measure_retrieval(service, request, question)
                    row.update(
                        index_id=details["index_id"], index_generation=details["published"]["generation"]
                    )
                except Timeout as exc:
                    row = {"status": "failed", "error": type(exc).__name__ + ": operation lock busy"}
                except Exception as exc:
                    log.exception("Evaluation task failed: %s", key)
                    row = {
                        "status": "failed",
                        "error": str(exc) if isinstance(exc, AppError) else type(exc).__name__,
                    }
                row.update(
                    question_id=qid,
                    language=questions[qid]["language"],
                    category=questions[qid]["category"],
                    difficulty=questions[qid]["difficulty"],
                    answerable=questions[qid]["answerable"],
                    split=manifest["split"],
                    run_id=run_id,
                    configuration_id=conf["id"],
                    configuration_label=label,
                    experiment=conf["family"],
                    parameters=parameters,
                    started_at=started,
                    finished_at=now(),
                )
                self.store.checkpoint(run_id, key, row, retry=retry)
                rows[key] = row
                print(f"{run_id} {len(rows)}/{len(tasks)} {key} {row['status']}", flush=True)
            errors = sum(r["status"] in RETRY for r in rows.values())
            state("completed_with_errors" if errors else "completed", failures=errors)
            return 1 if errors else 0
        except (StopRun, KeyboardInterrupt) as exc:
            state("cancelled" if isinstance(exc, StopRun) else "interrupted")
            return 130
        except Exception as exc:
            state("failed", error=str(exc) if isinstance(exc, AppError) else type(exc).__name__)
            raise
        finally:
            self.service.models.unload()
            for service in active_services | {self.service}:
                if service._chat is not None:
                    service.chat.tracing.flush()
