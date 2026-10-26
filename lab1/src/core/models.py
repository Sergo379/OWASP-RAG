"""Validated persistence and configuration contracts shared by API and CLI."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CorpusItem(StrictModel):
    path: str = Field(pattern=r"^cheatsheets/[A-Za-z0-9_-]+\.md$")
    topic: str = Field(min_length=1)


class CorpusConfig(StrictModel):
    repository: str = Field(default="OWASP/CheatSheetSeries", pattern=r"^[\w.-]+/[\w.-]+$")
    ref: str = "master"
    timeout: float = Field(default=20, gt=0, le=120)
    retries: int = Field(default=3, ge=0, le=5)
    max_backoff: float = Field(default=8, ge=0, le=30)
    max_document_bytes: int = Field(default=2_000_000, gt=0)
    documents: list[CorpusItem]

    @model_validator(mode="after")
    def unique_paths(self):
        if len({x.path for x in self.documents}) != len(self.documents):
            raise ValueError("Duplicate corpus paths")
        return self


class TokenizerConfig(StrictModel):
    model: str
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    max_input_tokens: int = 512
    reserved_tokens: int = 32


class PreprocessingConfig(StrictModel):
    pipeline_version: str = "1"
    strategy: Literal["fixed", "overlap", "paragraph", "section"] = "section"
    chunk_size: int = Field(default=256, ge=8, le=480)
    overlap: float = Field(default=0, ge=0, le=0.2)
    sizes: list[int] = [128, 256, 384]
    overlaps: list[float] = [0, 0.1, 0.2]
    tokenizer: TokenizerConfig

    @model_validator(mode="after")
    def budget(self):
        if self.chunk_size + self.tokenizer.reserved_tokens > self.tokenizer.max_input_tokens:
            raise ValueError("Chunk exceeds tokenizer input budget")
        if self.strategy != "overlap" and self.overlap != 0:
            raise ValueError("Only overlap strategy accepts nonzero overlap")
        return self


class Document(StrictModel):
    document_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    source: str
    source_path: str
    url: str
    title: str
    topic: str
    git_blob_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    repository_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    fetched_at: str
    updated_at: str | None = None
    last_checked_at: str
    status: Literal["active", "deleted", "out_of_scope"] = "active"


class Manifest(StrictModel):
    schema_version: Literal[1] = 1
    repository_commit: str | None = None
    last_updated_at: str | None = None
    documents: dict[str, Document] = {}

    @model_validator(mode="after")
    def matching_ids(self):
        if any(key != value.document_id for key, value in self.documents.items()):
            raise ValueError("Manifest document key mismatch")
        return self


class Chunk(StrictModel):
    chunk_id: str
    document_id: str
    title: str
    source_path: str
    url: str
    section: str
    heading_path: list[str]
    chunk_index: int
    chunking_strategy: str
    chunk_size: int
    overlap: float
    content_sha256: str
    source_commit: str
    token_count: int
    text: str
    tokenizer: str
    configuration_id: str


class PreprocessRequest(StrictModel):
    strategy: Literal["fixed", "overlap", "paragraph", "section"] = "section"
    chunk_size: Literal[128, 256, 384] = 256
    overlap: Literal[0.0, 0.1, 0.2] = 0.0
