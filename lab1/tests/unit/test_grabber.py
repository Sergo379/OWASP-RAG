import httpx
import pytest

from src.core.config import AppError
from src.core.models import CorpusItem
from src.grabber.github import GitHubGrabber
from src.storage.files import atomic_write, write_json


def grab(settings, store, client):
    return GitHubGrabber(settings.corpus, store, client=client, sleep=lambda _: None)


def test_initial_repeat_changed_stable_ids(settings, store, client, github):
    first = grab(settings, store, client).run()
    assert first["new"] == 1 and not first["errors"]
    doc = next(iter(store.manifest().documents.values()))
    assert doc.updated_at is None
    github.calls.clear()
    second = grab(settings, store, client).run()
    assert second["unchanged"] == 1 and len(github.calls) == 2
    assert len(list((store.root / "raw").glob("*.md"))) == 1
    github.files[doc.source_path] += b"\nNew guidance."
    github.commit = "b" * 40
    third = grab(settings, store, client).run()
    updated = store.manifest().documents[doc.document_id]
    assert third["changed"] == 1 and updated.content_sha256 != doc.content_sha256
    assert updated.repository_commit == github.commit


def test_deleted_keeps_raw(settings, store, client, github):
    grab(settings, store, client).run()
    doc = next(iter(store.manifest().documents.values()))
    github.files.clear()
    assert grab(settings, store, client).run()["deleted"] == 1
    assert store.manifest().documents[doc.document_id].status == "deleted"
    assert store.raw(doc.content_sha256)


def test_repairs_corrupt_local_blob(settings, store, client):
    grab(settings, store, client).run()
    doc = next(iter(store.manifest().documents.values()))
    atomic_write(store.raw_path(doc.content_sha256), b"broken")
    assert grab(settings, store, client).run()["changed"] == 1
    assert store.raw(doc.content_sha256).startswith(b"# Security")


@pytest.mark.parametrize("status", [404, 401, 403])
def test_http_errors(settings, store, status):
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(status))) as client:
        with pytest.raises(AppError, match=f"HTTP {status}"):
            grab(settings, store, client).request("https://api.github.com/test")


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_retry_then_success(settings, store, status):
    responses = [httpx.Response(status, headers={"Retry-After": "0"}), httpx.Response(200, content=b"ok")]
    with httpx.Client(transport=httpx.MockTransport(lambda _: responses.pop(0))) as client:
        assert grab(settings, store, client).request("https://api.github.com/test") == b"ok"
        assert not responses


@pytest.mark.parametrize("status", [429, 403])
def test_rate_limit_no_unbounded_wait(settings, store, status):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(status, headers={"Retry-After": "3600", "X-RateLimit-Remaining": "0"})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(AppError, match="rate limit"):
            grab(settings, store, client).request("https://api.github.com/test")
    assert len(calls) == 1


def test_timeout_retries_exhausted(settings, store):
    calls = []

    def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout("token-must-not-leak")

    with httpx.Client(transport=httpx.MockTransport(timeout)) as client:
        with pytest.raises(AppError, match="timeout") as error:
            grab(settings, store, client).request("https://api.github.com/test")
    assert len(calls) == settings.corpus.retries + 1
    assert "token-must-not-leak" not in str(error.value)


@pytest.mark.parametrize("body", [b"not json", b"{}", b'{"sha":"invalid"}'])
def test_corrupt_discovery(settings, store, body):
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body))) as client:
        with pytest.raises(AppError):
            grab(settings, store, client).run()
    assert not store.manifest_path.exists()


def test_integrity_mismatch_not_committed(settings, store, github):
    def respond(request):
        if request.url.host == "raw.githubusercontent.com":
            return httpx.Response(200, content=b"# Unexpected")
        return github(request)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = grab(settings, store, client).run()
    assert len(result["errors"]) == 1 and not store.manifest().documents


@pytest.mark.parametrize(
    "manifest", [{"documents": []}, {"schema_version": 999}, {"documents": {"wrong": {}}}]
)
def test_invalid_manifest_preserved(settings, store, client, github, manifest):
    write_json(store.manifest_path, manifest)
    before = store.manifest_path.read_bytes()
    with pytest.raises(AppError):
        grab(settings, store, client).run()
    assert store.manifest_path.read_bytes() == before and not github.calls


def test_interruption_resume(settings, store, github):
    path = "cheatsheets/Second_Cheat_Sheet.md"
    github.files[path] = b"# Second\ntext"
    settings.corpus.documents.append(CorpusItem(path=path, topic="Test"))

    def interrupted(request):
        if request.url.host == "raw.githubusercontent.com" and request.url.path.endswith(
            "Second_Cheat_Sheet.md"
        ):
            raise KeyboardInterrupt()
        return github(request)

    with httpx.Client(transport=httpx.MockTransport(interrupted)) as client:
        with pytest.raises(KeyboardInterrupt):
            grab(settings, store, client).run()
    assert len(store.manifest().documents) == 1
    with httpx.Client(transport=httpx.MockTransport(github)) as client:
        result = grab(settings, store, client).run()
    assert result["unchanged"] == 1 and result["new"] == 1


def test_atomic_failure_preserves_previous(tmp_path, monkeypatch):
    import src.storage.files as files

    path = tmp_path / "state.json"
    atomic_write(path, b"old")

    def fail(*args):
        raise OSError("disk error")

    monkeypatch.setattr(files.os, "replace", fail)
    with pytest.raises(OSError):
        atomic_write(path, b"new")
    assert path.read_bytes() == b"old" and not list(tmp_path.glob("*.tmp"))
