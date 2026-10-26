"""Validation and deterministic filtering rules, independent from model inference."""

from types import SimpleNamespace

import pytest

from src.retrieval.models import SearchRequest
from src.retrieval.pipeline import deduplicate, filter_candidates


@pytest.mark.parametrize("query", ["", "   ", "\x00invalid", "x" * 2001])
def test_query_validation(query):
    with pytest.raises(ValueError):
        SearchRequest(query=query, index_id="a" * 32)


def test_normalized_query_and_candidate_pool():
    request = SearchRequest(query="  security вопрос  ", index_id="a" * 32)
    assert request.query == "security вопрос"
    with pytest.raises(ValueError):
        SearchRequest(query="security", index_id="a" * 32, top_k=20, candidate_pool_size=20)


def candidate(chunk, text_hash, similarity=0.8, topic="Injection", doc="doc", source_hash="raw"):
    return {
        "vector_id": "vector-" + chunk,
        "vector_similarity": similarity,
        "metadata": {
            "chunk_id": chunk,
            "content_sha256": text_hash,
            "document_id": doc,
            "source_content_sha256": source_hash,
            "topic": topic,
        },
    }


def test_filter_reasons_are_distinct():
    manifest = SimpleNamespace(documents={"doc": SimpleNamespace(status="active", content_sha256="raw")})
    request = SearchRequest(query="SQL", index_id="a" * 32, topic="Injection", similarity_threshold=0.5)
    values = [
        candidate("kept", "a"),
        candidate("low", "b", similarity=0.2),
        candidate("topic", "c", topic="Authentication"),
        candidate("old", "d", source_hash="old"),
        candidate("deleted", "e", doc="missing"),
    ]
    kept, removed = filter_candidates(values, request, manifest)
    assert [c["metadata"]["chunk_id"] for c in kept] == ["kept"]
    assert [r["reason"] for r in removed] == ["below_threshold", "metadata_mismatch", "stale", "stale"]


def test_dedup_origin_chunk_and_identical_text_are_deterministic():
    values = [
        candidate("first", "text-a"),
        candidate("first", "text-b"),
        candidate("other-doc-same-text", "text-a"),
        candidate("new", "text-c"),
    ]
    kept, removed = deduplicate(values)
    assert [c["metadata"]["chunk_id"] for c in kept] == ["first", "new"]
    assert len(removed) == 2 and all(r["reason"] == "duplicate" for r in removed)
    assert (kept, removed) == deduplicate(values)
