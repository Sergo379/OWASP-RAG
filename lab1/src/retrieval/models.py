from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from src.core.models import StrictModel


class PrepareModelRequest(StrictModel):
    model: Literal["e5", "minilm", "reranker"]


class BuildIndexRequest(StrictModel):
    model: Literal["e5", "minilm"] = "e5"
    configuration_id: str | None = Field(None, pattern=r"^[a-f0-9]{20}$")
    heading_policy: Literal["include_all", "skip_heading_only"] = "include_all"
    overflow_policy: Literal["split", "error"] = "split"
    rebuild: bool = False


class SearchRequest(StrictModel):
    query: str = Field(min_length=1, max_length=2000)
    index_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    model: Literal["e5", "minilm"] = "e5"
    top_k: Literal[3, 5, 10, 20] = 5
    similarity_threshold: float = Field(0, ge=-1, le=1)
    topic: str | None = Field(None, max_length=200)
    document_id: str | None = Field(None, pattern=r"^[a-f0-9]{64}$")
    reranking: bool = False
    candidate_pool_size: int = Field(80, ge=20, le=500)

    @field_validator("query")
    @classmethod
    def valid_query(cls, value: str):
        value = value.strip()
        if not value or any(ord(c) < 32 and c not in "\t\r\n" for c in value):
            raise ValueError("Введите непустой текстовый запрос без управляющих символов")
        return value

    @model_validator(mode="after")
    def pool_budget(self):
        if self.candidate_pool_size < self.top_k * 2:
            raise ValueError("Candidate pool должен быть не меньше 2 × Top-K")
        return self


class SearchResult(StrictModel):
    vector_id: str
    chunk_id: str
    document_id: str
    title: str
    topic: str
    url: str
    section: str
    heading_path: list[str]
    text: str
    original_text: str
    retrieval_rank: int
    final_rank: int
    vector_distance: float
    vector_similarity: float
    rerank_score: float | None = None
    metadata: dict[str, Any]


class SearchResponse(StrictModel):
    request_id: str
    results: list[SearchResult]
    without_reranking: list[SearchResult]
    comparison: list[dict[str, Any]]
    diagnostics: dict[str, Any]


class OperationResponse(StrictModel):
    operation_id: str
    kind: str
    status: str
    started_at: str
    finished_at: str | None
    completed: int
    total: int
    current: str
    error: str | None
    result: dict | None
    # Progress fields are explicitly allowed for existing and model/index operations.
    model_config = {"extra": "allow"}


class ItemsResponse(StrictModel):
    items: list[dict[str, Any]]


class IndexResponse(StrictModel):
    index_id: str
    identity: dict[str, Any]
    created_at: str
    updated_at: str
    published: dict | None
    attempt: dict | None
    status: str
    searchable: bool
    disk_bytes: int
    actual_vectors_count: int | None = None
    inspection: dict | None = None
