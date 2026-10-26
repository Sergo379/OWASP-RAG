"""Real persistent Chroma + deliberately mocked model provider; no pretrained weights."""

import hashlib
import json
import re

import numpy as np
import pytest
from fastapi.testclient import TestClient
from filelock import FileLock

from src.api.app import create_app
from src.core.config import AppError
from src.core.service import Service
from src.grabber.github import GitHubGrabber
from src.preprocessing.pipeline import preprocess
from src.retrieval.models import BuildIndexRequest, SearchRequest
from src.storage.vectors import VectorStore


class MockProvider:
    def __init__(self, spec):
        self.spec, self.model_identity, self.dimension = spec, spec.identity, spec.dimension
        self.calls = 0
        self.fail = False

    def document_parts(self, text, policy="split"):
        return [
            {
                "text": text,
                "char_start": 0,
                "char_end": len(text),
                "model_input_tokens": len(text.split()) + 2,
            }
        ]

    def vector(self, text):
        vector = np.zeros(self.dimension, np.float32)
        for word in re.findall(r"\w+", text.lower()):
            vector[int(hashlib.sha256(word.encode()).hexdigest(), 16) % self.dimension] += 1
        vector[0] += 0.01
        return vector / np.linalg.norm(vector)

    def embed_documents(self, texts):
        self.calls += len(texts)
        if self.fail:
            raise AppError("Mock inference failure")
        return np.array([self.vector(t) for t in texts])

    def embed_query(self, query):
        return self.vector(query)

    def rerank(self, query, texts, *args):
        if self.fail:
            raise AppError("Mock reranker failure")
        return list(map(float, range(len(texts)))), [{"windows": 1} for _ in texts]


class MockModels:
    def __init__(self, config):
        self.providers = {key: MockProvider(spec) for key, spec in config.models.items()}
        self.loaded = {}
        self.gets = []
        self.missing = set()

    def get(self, key):
        self.gets.append(key)
        if key in self.missing:
            raise AppError("Модель отсутствует; Prepare Model")
        return self.providers[key]

    def unload(self, *args):
        pass


@pytest.fixture
def retrieval_service(settings, client, tokenizer, github):
    github.files[next(iter(github.files))] = (
        b"# Security\n\n## SQL\nParameterized SQL queries prevent injection.\n\n"
        b"## Passwords\nHash passwords using Argon2. Never store plaintext.\n\n"
        b"## Sessions\nSecure session cookies and CSRF tokens protect sessions.\n"
    )
    service = Service(settings)
    GitHubGrabber(settings.corpus, service.store, client=client).run()
    preprocess(service.store, settings.preprocessing, tokenizer)
    service.models = MockModels(service.retrieval_config)
    return service


def build(service, **options):
    request = BuildIndexRequest(**options)
    result = service.vectors.build(request, service.models)
    return result["index_id"], result


def test_chroma_persistence_repeat_metadata_and_snapshot(retrieval_service):
    service = retrieval_service
    index_id, first = build(service)
    provider = service.models.providers["e5"]
    calls = provider.calls
    repeat_id, repeat = build(service)
    assert repeat_id == index_id and repeat["reused"] and provider.calls == calls
    assert repeat["computed_vectors"] == 0 and repeat["reused_vectors"] == first["vectors_count"]
    fresh = VectorStore(service.store, service.retrieval_config, service.vectors.artifacts)
    record = fresh.catalog()["indexes"][index_id]
    collection = fresh.collection(record)
    assert collection.count() == first["vectors_count"]
    meta = collection.get(include=["metadatas"])["metadatas"][0]
    assert json.loads(meta["heading_path"]) and meta["topic"] == "Test"
    assert meta["model_identity"] == service.retrieval_config.models["e5"].identity
    assert meta["source_content_sha256"]
    assert fresh.inspect(index_id)["actual_vectors_count"] == first["vectors_count"]
    other_id, _ = build(service, model="minilm")
    assert other_id != index_id
    policy_id, _ = build(service, heading_policy="skip_heading_only")
    assert policy_id != index_id


@pytest.mark.parametrize("top_k", [3, 5, 10, 20])
def test_search_topk_scores_and_diagnostics(retrieval_service, top_k):
    service = retrieval_service
    index_id, _ = build(service)
    response = service.search(SearchRequest(query="SQL injection", index_id=index_id, top_k=top_k))
    assert 0 < len(response["results"]) <= top_k
    assert response["diagnostics"]["candidates_retrieved"] >= len(response["results"])
    assert "reranker" not in service.models.gets
    for rank, result in enumerate(response["results"], 1):
        assert result["final_rank"] == rank
        assert np.isclose(result["vector_similarity"], 1 - result["vector_distance"])
        assert result["rerank_score"] is None
    assert {s["event"] for s in response["diagnostics"]["stages"]} >= {"QUERY_RECEIVED", "RESULTS_READY"}


def test_filter_threshold_topic_stale_and_model_mismatch(retrieval_service):
    service = retrieval_service
    index_id, _ = build(service)
    for options in ({"topic": "No such topic"}, {"similarity_threshold": 1}):
        result = service.search(SearchRequest(query="SQL injection", index_id=index_id, **options))
        assert not result["results"]
    with pytest.raises(AppError, match="модель"):
        service.search(SearchRequest(query="test", index_id=index_id, model="minilm"))
    manifest = service.store.manifest()
    for doc in manifest.documents.values():
        doc.status = "deleted"
    service.store.save_manifest(manifest)
    response = service.search(SearchRequest(query="SQL", index_id=index_id))
    assert not response["results"] and response["diagnostics"]["stale_index"]
    assert all(r["reason"] == "stale" for r in response["diagnostics"]["removed"])
    assert service.vectors.lists()[0]["status"] == "Stale"


def test_reranking_preserves_original_scores_and_uses_same_pool(retrieval_service):
    service = retrieval_service
    index_id, _ = build(service)
    result = service.search(SearchRequest(query="SQL", index_id=index_id, reranking=True))
    assert result["comparison"] and result["diagnostics"]["reranking_applied"]
    assert any(c["original_rank"] != c["new_rank"] for c in result["comparison"])
    originals = {r["chunk_id"]: r for r in result["without_reranking"]}
    for row in result["results"]:
        assert row["rerank_score"] is not None
        if row["chunk_id"] in originals:
            assert row["vector_similarity"] == originals[row["chunk_id"]]["vector_similarity"]
    service.models.missing.add("reranker")
    with pytest.raises(AppError, match="Prepare Model"):
        service.search(SearchRequest(query="SQL", index_id=index_id, reranking=True))
    service.models.missing.clear()
    service.models.providers["reranker"].fail = True
    with pytest.raises(AppError, match="inference|reranker"):
        service.search(SearchRequest(query="SQL", index_id=index_id, reranking=True))


def test_failed_rebuild_preserves_publication_and_cache(retrieval_service, monkeypatch):
    service = retrieval_service
    index_id, first = build(service)
    record = service.vectors.catalog()["indexes"][index_id]
    client = service.vectors.client(index_id)
    original = client.get_or_create_collection

    def failing_collection(*args, **kwargs):
        collection = original(*args, **kwargs)
        monkeypatch.setattr(
            collection, "upsert", lambda **kw: (_ for _ in ()).throw(AppError("injected write failure"))
        )
        return collection

    monkeypatch.setattr(client, "get_or_create_collection", failing_collection)
    with pytest.raises(AppError):
        build(service, rebuild=True)
    saved = service.vectors.catalog()["indexes"][index_id]
    assert saved["published"]["collection_name"] == record["published"]["collection_name"]
    assert saved["attempt"]["status"] == "Failed"
    assert service.vectors.collection(saved).count() == first["vectors_count"]
    monkeypatch.setattr(client, "get_or_create_collection", original)
    _, resumed = build(service)
    assert resumed["computed_vectors"] == 0 and resumed["generation"] == 2


def test_interruption_recovery_and_invalid_index(retrieval_service):
    service = retrieval_service

    def interrupt(**fields):
        if fields.get("batch") == 1:
            raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        service.vectors.build(BuildIndexRequest(), service.models, interrupt)
    record = next(iter(service.vectors.catalog()["indexes"].values()))
    assert record["published"] is None and record["attempt"]["status"] == "Interrupted"
    index_id, result = build(service)
    assert result["generation"] == 1
    record = service.vectors.catalog()["indexes"][index_id]
    service.vectors.client(index_id).delete_collection(record["published"]["collection_name"])
    with pytest.raises(AppError, match="повреждён|отсутствует"):
        service.search(SearchRequest(query="SQL", index_id=index_id))


def test_stage2_api_and_busy_lock(retrieval_service):
    service = retrieval_service
    index_id, _ = build(service)
    with TestClient(create_app(service)) as web:
        assert web.get("/api/embeddings/models").status_code == 200
        assert len(web.get("/api/embeddings/models").json()["items"]) == 3
        assert web.get("/api/indexes").json()["items"]
        assert web.get("/api/indexes/unknown").status_code == 404
        assert web.get("/api/indexes/" + index_id).status_code == 200
        assert web.get("/api/chunks/analysis").json()["heading_only"] > 0
        payload = {"query": "SQL", "index_id": index_id}
        result = web.post("/api/search", json=payload)
        assert result.status_code == 200
        assert web.get("/api/search/" + result.json()["request_id"]).status_code == 200
        for query in ["", "  ", "\x00bad", "x" * 2001]:
            assert web.post("/api/search", json={**payload, "query": query}).status_code == 422
        assert (
            web.post("/api/search", json={**payload, "top_k": 20, "candidate_pool_size": 20}).status_code
            == 422
        )
        assert web.post("/api/embeddings/prepare", json={"model": "invalid"}).status_code == 422
        with FileLock(service.lock_path):
            assert web.post("/api/search", json=payload).status_code == 409
            assert web.post("/api/indexes/build", json={}).status_code == 409
        assert web.get("/static/stage2.js").status_code == 200
        assert b"Search Execution Details" in web.get("/").content
