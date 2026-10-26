import socket
import uuid
from pathlib import Path

import httpx
import pytest
from tokenizers import Tokenizer, models, pre_tokenizers

from src.core.config import Settings
from src.core.models import CorpusItem
from src.grabber.github import blob_sha
from src.preprocessing.tokenization import E5Tokenizer
from src.storage.files import Store


def pytest_configure(config):
    # Isolate each run from Windows temp symlinks left by other sandboxes/processes.
    # A fresh UUID path is never a pre-existing user directory.
    if config.option.basetemp is None:
        parent = Path(__file__).resolve().parents[1] / ".cache" / "tests"
        parent.mkdir(parents=True, exist_ok=True)
        config.option.basetemp = str(parent / uuid.uuid4().hex)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    original = socket.socket.connect

    def blocked(sock, address):
        # Windows asyncio implements socketpair through loopback TCP.
        if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
            return original(sock, address)
        raise AssertionError("Tests must not use a real network")

    monkeypatch.setattr(socket.socket, "connect", blocked)


@pytest.fixture
def settings(tmp_path):
    config = Settings(data_dir=tmp_path / "data")
    config.corpus.documents = [CorpusItem(path="cheatsheets/Test_Cheat_Sheet.md", topic="Test")]
    config.corpus.retries = 2
    config.corpus.max_backoff = 0
    return config


@pytest.fixture
def store(settings):
    return Store(settings.data_dir)


@pytest.fixture
def tokenizer():
    # Small local byte-level tokenizer; production always uses the pinned E5 tokenizer.
    backend = Tokenizer(
        models.BPE(vocab={c: i for i, c in enumerate(pre_tokenizers.ByteLevel.alphabet())}, merges=[])
    )
    backend.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False, use_regex=False)
    result = E5Tokenizer.__new__(E5Tokenizer)
    result.backend, result.identity = backend, "fixture-byte-bpe@1"
    return result


class FakeGitHub:
    def __init__(self):
        self.commit = "a" * 40
        self.files = {
            "cheatsheets/Test_Cheat_Sheet.md": b"# Security\n\n## SQL\nUse parameters.\n\n```sql\nSELECT * FROM users WHERE id = ?;\n```\n"
        }
        self.calls = []
        self.raw_error = None

    def __call__(self, request):
        self.calls.append(str(request.url))
        path = request.url.path
        if "/commits/" in path:
            return httpx.Response(200, json={"sha": self.commit})
        if "/git/trees/" in path:
            return httpx.Response(
                200,
                json={
                    "sha": self.commit,
                    "truncated": False,
                    "tree": [{"path": p, "sha": blob_sha(b), "type": "blob"} for p, b in self.files.items()],
                },
            )
        if self.raw_error:
            raise self.raw_error
        name = "cheatsheets/" + path.split("/cheatsheets/")[-1]
        return httpx.Response(200, content=self.files[name]) if name in self.files else httpx.Response(404)


@pytest.fixture
def github():
    return FakeGitHub()


@pytest.fixture
def client(github):
    with httpx.Client(transport=httpx.MockTransport(github)) as client:
        yield client
