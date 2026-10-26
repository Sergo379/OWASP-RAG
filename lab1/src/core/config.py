import os
from pathlib import Path

import yaml
from pydantic import ValidationError

from .models import CorpusConfig, PreprocessingConfig

ROOT = Path(__file__).resolve().parents[2]


class AppError(Exception):
    """Safe, user-facing error. Never contains request headers or secrets."""


def load_yaml(path: Path) -> dict:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Expected mapping")
        return value
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise AppError(f"Некорректная конфигурация: {path.name}") from exc


class Settings:
    def __init__(self, root: Path = ROOT, data_dir: Path | None = None):
        self.root = root
        self.data_dir = data_dir or Path(os.environ.get("OWASP_DATA_DIR") or root / "data")
        try:
            self.corpus = CorpusConfig.model_validate(load_yaml(root / "configs/corpus.yaml"))
            self.preprocessing = PreprocessingConfig.model_validate(
                load_yaml(root / "configs/preprocessing.yaml")
            )
        except ValidationError as exc:
            raise AppError("Некорректная конфигурация corpus.yaml или preprocessing.yaml") from exc
