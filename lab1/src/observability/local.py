import json
import logging
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path

from filelock import FileLock, Timeout

from src.core.config import load_yaml
from src.storage.files import now


class JsonFormatter(logging.Formatter):
    def format(self, record):
        result = {
            "timestamp": now(),
            "level": record.levelname,
            "module": record.name,
            "operation": getattr(record, "operation", "application"),
            "message": record.getMessage(),
        }
        if record.exc_info:
            # Retain diagnostic call locations without exception messages, code or local values.
            result["error_details"] = {
                "type": record.exc_info[0].__name__,
                "frames": [
                    {"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
                    for frame in traceback.extract_tb(record.exc_info[2])
                ],
            }
        if hasattr(record, "context"):
            result["context"] = record.context
        return json.dumps(result, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    def format(self, record):
        context = getattr(record, "context", {})
        keys = (
            "request_id",
            "model",
            "model_identity",
            "index_id",
            "dimension",
            "duration_ms",
            "total_ms",
            "top_k",
            "pool_size",
            "candidates",
            "kept",
            "returned",
            "enabled",
        )
        fields = [f"{key}={context[key]}" for key in keys if key in context]
        if isinstance(context.get("removed"), list):
            fields.append(f"removed={len(context['removed'])}")
        if isinstance(context.get("ranking"), list):
            fields.append(f"reranked={len(context['ranking'])}")
        return f"[{record.name}] {record.getMessage()} {' '.join(fields)}".rstrip()


class SharedRotatingFileHandler(RotatingFileHandler):
    """CLI + API rotation on Windows: no persistent open handles between writes."""
    def emit(self, record):
        try:
            with FileLock(self.baseFilename+".lock", timeout=2):
                try:
                    super().emit(record)
                finally:
                    if self.stream:
                        self.stream.close()
                        self.stream = None
        except Timeout:
            # A blocked logging writer must not block core RAG indefinitely.
            pass

    def doRollover(self):
        try:
            super().doRollover()
        except PermissionError:
            # A viewer or a pre-upgrade process can still hold the old log open.
            # Append this record and retry rotation on the next write.
            pass


def configure(root: Path):
    logger = logging.getLogger("owasp")
    if logger.handlers:
        return
    config = load_yaml(root / "configs/logging.yaml")
    folder = root / "logs"
    folder.mkdir(exist_ok=True)
    handler = SharedRotatingFileHandler(
        folder / "application.log",
        maxBytes=config["max_bytes"],
        backupCount=config["backup_count"],
        encoding="utf-8",
        delay=True,
    )
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    console = logging.StreamHandler()
    console.setFormatter(ConsoleFormatter())
    logger.addHandler(console)
    logger.setLevel(config["level"])
