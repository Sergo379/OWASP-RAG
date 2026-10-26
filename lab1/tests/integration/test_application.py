import threading
import time

from fastapi.testclient import TestClient

from src.api.app import create_app
from src.core.config import AppError
from src.core.service import Service
from src.grabber.github import GitHubGrabber
from src.preprocessing.pipeline import preprocess
from src.storage.files import write_json


def services(settings, client, tokenizer):
    return Service(
        settings,
        lambda c, s: GitHubGrabber(c, s, client=client, sleep=lambda _: None),
        lambda *args: tokenizer,
    )


def wait(client):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        op = client.get("/api/operations/current").json()["operation"]
        if op and op["status"] != "running":
            return op
        time.sleep(0.01)
    raise AssertionError("Operation failed to finish")


def test_empty_api(settings):
    with TestClient(create_app(Service(settings))) as web:
        assert web.get("/api/health").json()["status"] == "ok"
        assert web.get("/api/stats").json()["chunks"] == 0
        assert web.get("/api/documents").json() == {"items": []}
        assert web.get("/api/documents/missing").status_code == 404
        assert web.get("/").status_code == 200
        assert web.get("/static/app.js").status_code == 200
        assert web.post("/api/operations/preprocess", json={}).status_code == 202
        assert wait(web)["status"] == "failed"
        assert web.get("/api/errors").json()["items"]
        assert web.post("/api/operations/preprocess", json={"chunk_size": 999}).status_code == 422
        assert web.post("/api/operations/grab", headers={"Origin": "https://evil.example"}).status_code == 403


def test_full_pipeline_restart_and_metadata(settings, client, tokenizer):
    service = services(settings, client, tokenizer)
    with TestClient(create_app(service)) as web:
        started = web.post("/api/operations/grab")
        assert started.status_code == 202 and wait(web)["status"] == "succeeded"
        assert web.post("/api/operations/preprocess", json={}).status_code == 202
        assert wait(web)["status"] == "succeeded"
        stats = web.get("/api/stats").json()
        assert stats["downloaded_documents"] == stats["processed_documents"] == 1
        doc = web.get("/api/documents").json()["items"][0]
        chunks = web.get(f"/api/documents/{doc['document_id']}/chunks").json()["items"]
        assert chunks and chunks[0]["token_count"] == tokenizer.count(chunks[0]["text"])
        assert chunks[0]["source_path"] == doc["source_path"]
        assert chunks[0]["source_commit"] == doc["repository_commit"]
        assert chunks[0]["document_id"] == doc["document_id"]
        assert "SELECT" in web.get(f"/api/documents/{doc['document_id']}").json()["text"]
        first_ids = [c["chunk_id"] for c in chunks]
        result = service.start("preprocess", background=False)
        assert result["status"] == "succeeded"
        assert [c["chunk_id"] for c in service.store.chunks(service.store.run())] == first_ids
    restarted = services(settings, client, tokenizer)
    assert restarted.stats()["chunks"] == stats["chunks"]
    assert restarted.operation()["status"] == "succeeded"


def test_separate_configurations_and_stale_counts(settings, client, tokenizer, github):
    service = services(settings, client, tokenizer)
    service.start("grab", background=False)
    for strategy in ("fixed", "overlap", "paragraph", "section"):
        config = settings.preprocessing.model_copy(
            update={"strategy": strategy, "overlap": 0.1 if strategy == "overlap" else 0}
        )
        preprocess(service.store, config, tokenizer)
    assert len(service.store.processed_index()["runs"]) == 4
    assert len(list((service.store.root / "processed").glob("*.jsonl"))) == 4
    assert service.stats()["processed_documents"] == 1
    key = next(iter(github.files))
    github.files[key] += b"Changed content"
    service.start("grab", background=False)
    assert service.stats()["processed_documents"] == service.stats()["chunks"] == 0


def test_conflicting_cli_and_api_operations(settings):
    entered, release = threading.Event(), threading.Event()

    class WaitingGrabber:
        def __init__(self, *args):
            pass

        def run(self, progress):
            entered.set()
            assert release.wait(5)
            return {"new": 0}

    service = Service(settings, grabber_factory=WaitingGrabber)
    with TestClient(create_app(service)) as web:
        assert web.post("/api/operations/grab").status_code == 202
        assert entered.wait(1)
        try:
            assert web.post("/api/operations/grab").status_code == 409
            other = Service(settings)
            try:
                other.start("grab", background=False)
                raise AssertionError("Should reject concurrent CLI operation")
            except AppError as exc:
                assert "Уже" in str(exc)
        finally:
            release.set()
        assert wait(web)["status"] == "succeeded"


def test_recover_interrupted_state(settings):
    service = Service(settings)
    write_json(service.operation_path, {"operation_id": "old", "status": "running"})
    assert service.operation()["status"] == "interrupted"


def test_failed_network_is_visible_and_recovers(settings, client, tokenizer, monkeypatch):
    class FailedGrabber:
        def __init__(self, *args):
            pass

        def run(self, progress):
            raise AppError("GitHub недоступен: timeout или ошибка сети")

    service = Service(settings, grabber_factory=FailedGrabber)
    with TestClient(create_app(service)) as web:
        assert web.post("/api/operations/grab").status_code == 202
        assert wait(web)["error"].startswith("GitHub недоступен")
        assert web.get("/api/errors").json()["items"][0]["operation"] == "grab"
        service.grabber_factory = lambda c, s: GitHubGrabber(c, s, client=client)
        assert web.post("/api/operations/grab").status_code == 202
        assert wait(web)["status"] == "succeeded"
        assert web.get("/api/errors").json()["items"]  # Previous error remains discoverable.
