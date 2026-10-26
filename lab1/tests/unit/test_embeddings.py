from types import SimpleNamespace

import httpx
import numpy as np
import pytest

from src.core.config import AppError
from src.embeddings.config import ModelSpec, load_retrieval
from src.embeddings.download import ModelFiles
from src.embeddings.provider import HFProvider, ModelManager, validate_vectors
from src.storage.files import digest
from src.storage.vectors import EmbeddingCache, heading_only


@pytest.fixture
def tiny_provider(settings):
    """Tiny random local BERT verifies mechanics, never claimed as semantic smoke."""
    import torch
    from tokenizers import Tokenizer, models, pre_tokenizers, processors
    from transformers import BertConfig, BertModel, PreTrainedTokenizerFast

    vocabulary = {
        word: i
        for i, word in enumerate(
            ["[UNK]", "[PAD]", "[CLS]", "[SEP]", "query:", "passage:", "hello", "security", "code"]
        )
    }
    backend = Tokenizer(models.WordLevel(vocabulary, unk_token="[UNK]"))
    backend.pre_tokenizer = pre_tokenizers.WhitespaceSplit()
    backend.post_processor = processors.TemplateProcessing(
        single="[CLS] $A [SEP]",
        pair="[CLS] $A [SEP] $B:1 [SEP]:1",
        special_tokens=[("[CLS]", 2), ("[SEP]", 3)],
    )
    provider = HFProvider.__new__(HFProvider)
    provider.tokenizer = PreTrainedTokenizerFast(
        tokenizer_object=backend, unk_token="[UNK]", pad_token="[PAD]", cls_token="[CLS]", sep_token="[SEP]"
    )
    config, _ = load_retrieval(settings.root)
    provider.spec = config.models["e5"].model_copy(update={"dimension": 8, "max_input_tokens": 16})
    provider.dimension, provider.max_input_tokens = 8, 16
    provider.model_identity = provider.spec.identity
    torch.manual_seed(13)
    provider.model = BertModel(
        BertConfig(
            vocab_size=len(vocabulary),
            hidden_size=8,
            num_hidden_layers=1,
            num_attention_heads=2,
            intermediate_size=16,
            max_position_embeddings=32,
        )
    ).eval()
    return provider


def test_pinned_config_and_model_identity(settings):
    config, artifacts = load_retrieval(settings.root)
    assert config.models["e5"].query_prefix == "query: "
    assert config.models["e5"].passage_prefix == "passage: "
    assert config.models["minilm"].query_prefix == config.models["minilm"].passage_prefix == ""
    assert config.models["minilm"].max_input_tokens == 128
    assert len({m.identity for m in config.models.values()}) == 3
    assert all(any(a["path"] == "model.safetensors" for a in artifacts[k]) for k in config.models)
    with pytest.raises(ValueError):
        ModelSpec.model_validate({**config.models["e5"].model_dump(), "revision": "main"})


def test_prefixes_tokens_and_deterministic_normalization(tiny_provider):
    p = tiny_provider
    assert p.tokenizer.encode("query: hello") != p.tokenizer.encode("passage: hello")
    assert p.input_count("hello", True) == 4  # prefix + text + two special tokens
    first, second = p.embed_documents(["hello security"]), p.embed_documents(["hello security"])
    assert first.shape == (1, 8) and np.isfinite(first).all()
    np.testing.assert_allclose(first, second, atol=1e-6)
    assert np.isclose(np.linalg.norm(first), 1, atol=1e-6)
    assert p.embed_query("hello").shape == (8,)


def test_overflow_split_preserves_original(tiny_provider):
    text = "hello security code " * 40
    parts = tiny_provider.document_parts(text)
    assert len(parts) > 1 and "".join(p["text"] for p in parts) == text
    assert all(p["model_input_tokens"] <= 16 for p in parts)
    with pytest.raises(AppError, match="budget"):
        tiny_provider.document_parts(text, "error")
    with pytest.raises(AppError, match="budget"):
        tiny_provider.embed_query(text)


@pytest.mark.parametrize("vectors", [[[float("nan"), 0]], [[float("inf"), 1]], [[0, 0]], [[1, 2, 3]], [1, 2]])
def test_reject_invalid_vectors(vectors):
    with pytest.raises(AppError):
        validate_vectors(vectors, 2)


def test_download_integrity_offline_and_resume(settings, tmp_path):
    config, _ = load_retrieval(settings.root)
    contents = {name: name.encode() for name in ("config.json", "model.safetensors")}
    artifacts = {
        key: [{"path": name, "sha256": digest(data), "size": len(data)} for name, data in contents.items()]
        for key in config.models
    }
    files = ModelFiles(tmp_path, config, artifacts)

    def respond(request):
        return httpx.Response(200, content=contents[request.url.path.rsplit("/", 1)[-1]])

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        files.prepare("e5", client=client)
        assert files.ready("e5", verify=True)
        assert files.prepare("e5", client=client)["disk_bytes"] == sum(map(len, contents.values()))
    folder = files.folder("minilm")
    folder.mkdir(parents=True)
    (folder / "config.json").write_bytes(contents["config.json"])

    def offline(request):
        raise httpx.ConnectError("private-token-do-not-log")

    with httpx.Client(transport=httpx.MockTransport(offline)) as client:
        # Keep test fast while retaining bounded retry behavior.
        import unittest.mock

        with unittest.mock.patch("src.embeddings.download.time.sleep"):
            with pytest.raises(AppError, match="интернет") as exc:
                files.prepare("minilm", client=client)
    assert "private-token" not in str(exc.value)
    assert not files.ready("minilm")
    assert (folder / "config.json").read_bytes() == contents["config.json"]


def test_model_switch_retains_only_one_embedding(settings, tmp_path):
    config, _ = load_retrieval(settings.root)
    ready = {"e5", "minilm", "reranker"}
    files = SimpleNamespace(
        config=config, ready=lambda key, verify: key in ready, folder=lambda key: tmp_path / key
    )
    created = []

    def factory(spec, *args):
        created.append(spec.identity)
        return SimpleNamespace(spec=spec)

    manager = ModelManager(files, factory)
    assert manager.get("e5") is manager.get("e5") and len(created) == 1
    manager.get("minilm")
    assert manager.loaded["embedding"][0] == "minilm" and len(manager.loaded) == 1
    manager.get("reranker")
    assert len(manager.loaded) == 2
    manager.unload("reranker")
    assert len(manager.loaded) == 1
    ready.clear()
    with pytest.raises(AppError, match="Prepare Model"):
        manager.get("e5")


@pytest.mark.parametrize(
    "text, expected",
    [
        ("## Primary Defenses", True),
        ("# A\n## B", True),
        ("## A\nUseful body", False),
        ("```\n# code\n```", False),
        ("", False),
        ("#hashtag", False),
    ],
)
def test_heading_only(text, expected):
    assert heading_only(text) == expected


def test_embedding_cache_integrity(tmp_path):
    cache = EmbeddingCache(tmp_path / "cache.sqlite")
    vector = validate_vectors([[1, 2, 3]], 3)[0]
    cache.put("a", vector)
    cache.commit()
    np.testing.assert_array_equal(cache.get("a", 3), vector)
    assert cache.get("a", 4) is None
    cache.db.execute("UPDATE embeddings SET sha='bad'")
    assert cache.get("a", 3) is None
    cache.close()
