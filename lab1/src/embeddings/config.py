from typing import Literal

from pydantic import Field, model_validator

from src.core.config import AppError, load_yaml
from src.core.models import StrictModel
from src.storage.files import digest, read_json


class ModelSpec(StrictModel):
    repository: str = Field(pattern=r"^[\w.-]+/[\w.-]+$")
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    kind: Literal["embedding", "reranker"]
    dimension: int = Field(gt=0)
    max_input_tokens: int = Field(ge=8, le=512)
    query_prefix: str = ""
    passage_prefix: str = ""
    license: str

    @property
    def identity(self) -> str:
        return f"{self.repository}@{self.revision}"


class RetrievalConfig(StrictModel):
    schema_version: Literal[1] = 1
    batch_size: int = Field(8, ge=1, le=32)
    cpu_threads: int = Field(4, ge=1, le=12)
    short_chunk_tokens: int = Field(32, ge=1)
    heading_policy: Literal["include_all", "skip_heading_only"] = "include_all"
    overflow_policy: Literal["split", "error"] = "split"
    candidate_pool_size: int = Field(80, ge=20, le=500)
    max_candidate_pool: int = Field(500, ge=20, le=1000)
    query_max_chars: int = Field(2000, ge=1, le=10000)
    reranker_passage_tokens: int = Field(320, ge=32, le=384)
    log_queries: bool = False
    models: dict[str, ModelSpec]

    @model_validator(mode="after")
    def models_valid(self):
        if set(self.models) != {"e5", "minilm", "reranker"}:
            raise ValueError("Expected e5, minilm and reranker")
        if self.candidate_pool_size > self.max_candidate_pool:
            raise ValueError("Candidate pool exceeds maximum")
        return self


def load_retrieval(root):
    try:
        config = RetrievalConfig.model_validate(load_yaml(root / "configs/retrieval.yaml"))
        artifacts = read_json(root / "configs/model_artifacts.json")
        for key in config.models:
            if {a["path"] for a in artifacts[key]} != {
                "config.json",
                "model.safetensors",
                "tokenizer.json",
                "tokenizer_config.json",
                "special_tokens_map.json",
            }:
                raise ValueError("Unexpected model files")
        return config, artifacts
    except (ValueError, KeyError, TypeError) as exc:
        raise AppError("Некорректная конфигурация retrieval/models") from exc


def fingerprint(value: dict) -> str:
    import json

    return digest(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode())
