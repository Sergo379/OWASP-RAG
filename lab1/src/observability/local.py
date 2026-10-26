import json
import logging
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path

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
        return json.dumps(result, ensure_ascii=False)


def configure(root: Path):
    logger = logging.getLogger("owasp")
    if logger.handlers:
        return
    config = load_yaml(root / "configs/logging.yaml")
    folder = root / "logs"
    folder.mkdir(exist_ok=True)
    handler = RotatingFileHandler(
        folder / "application.log",
        maxBytes=config["max_bytes"],
        backupCount=config["backup_count"],
        encoding="utf-8",
    )
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(config["level"])
