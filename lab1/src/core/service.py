"""Application services: one mutation lock shared by every CLI/API process."""

import logging
import threading
import uuid

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
        if kind not in ("grab", "preprocess", "tokenizer"):
            raise AppError("Неизвестная операция")
        config = self.settings.preprocessing.model_dump()
        if options:
            config.update(options)
        from src.core.models import PreprocessingConfig

        try:
            config = PreprocessingConfig.model_validate(config)
        except ValueError as exc:
            raise AppError("Некорректные параметры preprocessing") from exc
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
                # Retain compact, durable operation history and last errors across successful runs.
                history_path = self.store.root / "manifests/history.json"
                history = read_json(history_path, [])
                history.append(operation)
                write_json(history_path, history[-50:])
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
            "stage": 1,
            "operation": self.operation(),
            "components": {
                "ChromaDB": "Ожидает этап №2",
                "Ollama Generation": "Ожидает этап №3",
                "Langfuse": "Ожидает настройку на этапе №3",
                "Evaluation": "Ожидает этап №4",
            },
        }
