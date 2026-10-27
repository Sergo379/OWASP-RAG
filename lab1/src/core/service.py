"""Application services: one mutation lock shared by every CLI/API process."""

import logging
import threading
import time
import uuid
from collections import OrderedDict

from filelock import FileLock, Timeout

from src import __version__
from src.core.config import AppError, Settings
from src.grabber.github import GitHubGrabber
from src.preprocessing.pipeline import preprocess
from src.preprocessing.tokenization import E5Tokenizer
from src.storage.files import Store, now, read_json, write_json

log = logging.getLogger("owasp.service")


class BusyError(AppError):
    pass


class Service:
    def __init__(self, settings: Settings, grabber_factory=GitHubGrabber, tokenizer_factory=E5Tokenizer):
        self.settings = settings
        self.store = Store(settings.data_dir)
        self.grabber_factory, self.tokenizer_factory = grabber_factory, tokenizer_factory
        self.operation_path = self.store.root / "manifests/operation.json"
        self.lock_path = self.store.root / ".operation.lock"
        from src.embeddings.config import load_retrieval
        from src.embeddings.download import ModelFiles
        from src.embeddings.provider import ModelManager
        from src.storage.vectors import VectorStore

        self.retrieval_config, artifacts = load_retrieval(settings.root)
        self.model_files = ModelFiles(self.store.root / "models", self.retrieval_config, artifacts)
        self.models = ModelManager(self.model_files)
        self.vectors = VectorStore(self.store, self.retrieval_config, artifacts)
        self.search_history = OrderedDict()
        self._chat = None
        self._chat_init_lock = threading.Lock()

    @property
    def chat(self):
        if self._chat is None:
            with self._chat_init_lock:
                if self._chat is None:
                    from src.generation.service import Chat
                    self._chat = Chat(self)
        return self._chat

    def operation(self) -> dict | None:
        operation = read_json(self.operation_path, None)
        if operation and operation["status"] == "running":
            try:
                with FileLock(self.lock_path, timeout=0):
                    # No live owner: previous process was killed, its OS lock is gone.
                    operation = read_json(self.operation_path)
                    if operation["status"] == "running":
                        operation.update(
                            status="interrupted",
                            finished_at=now(),
                            error="Операция прервана. Повторный запуск безопасен.",
                        )
                        write_json(self.operation_path, operation)
            except Timeout:
                pass
        return operation

    def start(self, kind: str, options: dict | None = None, background: bool = True) -> dict:
        if kind not in ("grab", "preprocess", "tokenizer", "prepare-model", "build-index"):
            raise AppError("Неизвестная операция")
        from src.core.models import PreprocessingConfig
        from src.retrieval.models import BuildIndexRequest, PrepareModelRequest

        try:
            if kind == "prepare-model":
                config = PrepareModelRequest.model_validate(options or {})
            elif kind == "build-index":
                config = BuildIndexRequest.model_validate(
                    {
                        "heading_policy": self.retrieval_config.heading_policy,
                        "overflow_policy": self.retrieval_config.overflow_policy,
                        **(options or {}),
                    }
                )
            else:
                config = PreprocessingConfig.model_validate(
                    {**self.settings.preprocessing.model_dump(), **(options or {})}
                )
        except ValueError as exc:
            raise AppError("Некорректные параметры операции") from exc
        lock = FileLock(self.lock_path, timeout=0, thread_local=False)
        try:
            lock.acquire()
        except Timeout as exc:
            raise BusyError("Уже выполняется операция. Дождитесь завершения.") from exc
        operation = {
            "operation_id": uuid.uuid4().hex,
            "kind": kind,
            "status": "running",
            "started_at": now(),
            "finished_at": None,
            "completed": 0,
            "total": 0,
            "current": "Подготовка",
            "error": None,
            "result": None,
        }
        try:
            operation["parameters"] = config.model_dump()
            write_json(self.operation_path, operation)
            if background:
                threading.Thread(target=self._execute, args=(operation, config, lock), daemon=True).start()
            else:
                self._execute(operation, config, lock)
        except BaseException:
            lock.release()
            raise
        return dict(operation)

    def _execute(self, operation, config, lock):
        kind = operation["kind"]

        def progress(**fields):
            operation.update(fields)
            write_json(self.operation_path, operation)

        try:
            log.info("Operation started", extra={"operation": kind})
            if kind == "grab":
                result = self.grabber_factory(self.settings.corpus, self.store).run(progress)
            elif kind == "prepare-model":
                import psutil

                operation["model"] = config.model
                result = self.model_files.prepare(config.model, progress)
                progress(current="Загрузка weights в CPU и проверка inference")
                before = time.perf_counter()
                self.models.unload(self.retrieval_config.models[config.model].kind)
                provider = self.models.get(config.model)
                if config.model == "reranker":
                    provider.rerank("security", ["Use secure configuration."], 1, 320)
                else:
                    provider.embed_query("security")
                result.update(
                    load_and_inference_ms=round((time.perf_counter() - before) * 1000, 2),
                    rss_bytes=psutil.Process().memory_info().rss,
                    inference_verified=True,
                )
            elif kind == "build-index":
                result = self.vectors.build(config, self.models, progress)
            else:
                # Empty corpus preprocessing must not trigger an unnecessary network download.
                if kind == "preprocess" and not any(
                    d.status == "active" for d in self.store.manifest().documents.values()
                ):
                    raise AppError("Корпус пуст. Сначала запустите Grabber.")
                progress(current="Загрузка токенизатора (при первом запуске нужна сеть)")
                tokenizer = self.tokenizer_factory(config.tokenizer, self.store.root / "tokenizers")
                result = (
                    {"tokenizer": tokenizer.identity}
                    if kind == "tokenizer"
                    else preprocess(self.store, config, tokenizer, progress)
                )
            operation.update(status="partial" if result.get("errors") else "succeeded", result=result)
        except AppError as exc:
            operation.update(status="failed", error=str(exc))
            log.warning("%s", exc, extra={"operation": kind})
        except Exception:
            operation.update(status="failed", error="Внутренняя ошибка. Подробности: logs/application.log")
            log.exception("Unexpected operation failure", extra={"operation": kind})
        except BaseException:
            operation.update(status="interrupted", error="Операция прервана; повторите запуск")
            raise
        finally:
            try:
                operation["finished_at"] = now()
                # Preserve the stage-1 history; stage 2 never truncates existing operation records.
                history_path = self.store.root / "manifests/history.json"
                history = read_json(history_path, [])
                history.append(operation)
                write_json(history_path, history)
                write_json(self.operation_path, operation)
            finally:
                lock.release()

    def documents(self) -> list[dict]:
        run = self.store.run()
        summaries = run["documents"] if run else {}
        result = []
        for doc in self.store.manifest().documents.values():
            summary = summaries.get(doc.document_id)
            current = bool(
                summary and summary["content_sha256"] == doc.content_sha256 and doc.status == "active"
            )
            result.append(
                {
                    **doc.model_dump(),
                    "chunks": summary["chunks"] if current else 0,
                    "processed": current,
                    "processing_status": "current" if current else "pending",
                }
            )
        return sorted(result, key=lambda d: d["title"].casefold())

    def stats(self) -> dict:
        manifest = self.store.manifest()
        docs = self.documents()
        run = self.store.run()
        return {
            "downloaded_documents": len(docs),
            "active_documents": sum(d["status"] == "active" for d in docs),
            "processed_documents": sum(d["processed"] for d in docs),
            "chunks": sum(d["chunks"] for d in docs),
            "last_updated_at": manifest.last_updated_at,
            "repository_commit": manifest.repository_commit,
            "configuration_id": run["configuration_id"] if run else None,
            "configuration": run["configuration"] if run else None,
            "available_configurations": list(self.store.processed_index()["runs"]),
        }

    def errors(self) -> list[dict]:
        result = []
        for op in reversed(read_json(self.store.root / "manifests/history.json", [])):
            if op.get("error"):
                result.append(
                    {"timestamp": op["finished_at"], "operation": op["kind"], "message": op["error"]}
                )
            for error in (op.get("result") or {}).get("errors", []):
                result.append({"timestamp": op["finished_at"], "operation": op["kind"], **error})
        return result[:20]

    def state(self) -> dict:
        return {
            "name": "OWASP-RAG",
            "version": __version__,
            "stage": 3,
            "operation": self.operation(),
            "components": {
                "ChromaDB": f"Persistent · индексов: {len(self.vectors.catalog()['indexes'])}",
                "Semantic Retrieval": "Доступен на странице Search",
                "Ollama Generation": "AI Chat · Qwen3 1.7B / 4B · CPU · llm-check проверяет API",
                "Langfuse": "OpenTelemetry SDK · Cloud: настройте локальный .env и langfuse-check",
                "Evaluation": "Ожидает этап №4",
            },
        }

    def search(self, request) -> dict:
        from src.retrieval.pipeline import execute_search

        try:
            with FileLock(self.lock_path, timeout=0):
                result = execute_search(request, self.vectors, self.models, self.retrieval_config)
                cutoff = time.monotonic() - 600
                for key in list(self.search_history):
                    if self.search_history[key][0] < cutoff:
                        del self.search_history[key]
                self.search_history[result["request_id"]] = (time.monotonic(), result)
                while len(self.search_history) > 20:
                    self.search_history.popitem(last=False)
                return result
        except Timeout as exc:
            raise BusyError("Ресурсы заняты другой операцией. Повторите поиск после её завершения.") from exc

    def model_statuses(self):
        operation = self.operation()
        items = self.model_files.statuses()
        loaded = list(self.models.loaded.values())
        for item in items:
            item["loaded_in_memory"] = any(key == item["key"] for key, _ in loaded)
            if item["status"] == "downloading" and not (
                operation and operation["status"] == "running" and operation.get("model") == item["key"]
            ):
                item["status"] = "interrupted"
        return items
