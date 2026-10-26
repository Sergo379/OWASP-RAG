from pathlib import Path

import httpx
import pytest

from src.core.config import AppError
from src.core.models import PreprocessingConfig
from src.preprocessing.chunking import split, windows
from src.preprocessing.markdown import clean_markdown
from src.preprocessing.tokenization import E5Tokenizer


def test_cleanup_preserves_technical_content():
    raw = (Path(__file__).parents[1] / "fixtures/technical.md").read_text(encoding="utf-8")
    cleaned, sections = clean_markdown(raw)
    assert "remove this navigation" not in cleaned and "unused: navigation" not in cleaned
    assert "SELECT * FROM users WHERE name = '<script>' AND id = ?;" in cleaned
    assert "<!-- This is literal code, keep it -->" in cleaned
    assert "- Validate user input." in cleaned and "Content-Security-Policy:" in cleaned
    assert "Русский текст: ё" in cleaned and "print('keep  spaces')" in cleaned
    assert sections[-1].heading_path == ["Technical Security", "Injection", "Unicode"]


def test_normalization_and_fences():
    cleaned, sections = clean_markdown(
        "\ufeff# Title\r\n\r\n\r\nBody  \r\n~~~sql\r\n# not heading\r\n\r\nSELECT  1;  \r\n~~~\r\n## Real\r\ntext"
    )
    assert "\r" not in cleaned and "\n\n\n" not in cleaned
    assert "SELECT  1;  " in cleaned
    assert [s.heading_path for s in sections] == [["Title"], ["Title", "Real"]]


def test_inline_code_is_literal():
    text = "Use `<!-- literal -->` and ``![literal](x)``. <!-- remove -->"
    cleaned, _ = clean_markdown(text)
    assert "`<!-- literal -->`" in cleaned and "``![literal](x)``" in cleaned
    assert "remove" not in cleaned


@pytest.mark.parametrize("strategy", ["fixed", "overlap", "paragraph", "section"])
@pytest.mark.parametrize("size", [128, 256, 384])
@pytest.mark.parametrize(
    "raw",
    ["", "short", "# A\n\n" + "security SQL < > \n" * 100, "# A\n## B\n### C\n" + "Безопасность! " * 150],
    ids=["empty", "short", "long", "unicode"],
)
def test_chunk_budget_determinism(strategy, size, raw, settings, tokenizer):
    config = settings.preprocessing.model_copy(
        update={"strategy": strategy, "chunk_size": size, "overlap": 0.2 if strategy == "overlap" else 0}
    )
    cleaned, sections = clean_markdown(raw)
    first = list(split(cleaned, sections, config, tokenizer))
    assert first == list(split(cleaned, sections, config, tokenizer))
    assert all(p.text.strip() and 0 < tokenizer.count(p.text) <= size for p in first)
    assert bool(first) == bool(raw.strip())


@pytest.mark.parametrize("overlap", [0, 0.1, 0.2])
def test_overlap_contents(tokenizer, overlap):
    text = "abcdefghijklmnopqrstuvwxyz" * 15
    chunks = list(windows(text, tokenizer, 128, overlap))
    assert len(chunks[0]) == 128
    amount = int(128 * overlap)
    if amount:
        assert chunks[0][-amount:] == chunks[1][:amount]
    else:
        assert "".join(chunks) == text


def test_paragraph_and_section_boundaries(settings, tokenizer):
    text, sections = clean_markdown(
        "# A\n\nFirst paragraph.\n\nSecond paragraph.\n\n## B\n\nThird paragraph."
    )
    config = settings.preprocessing.model_copy(update={"strategy": "section"})
    pieces = list(split(text, sections, config, tokenizer))
    assert len(pieces) == 2 and pieces[-1].heading_path == ["A", "B"]
    config = config.model_copy(update={"strategy": "paragraph", "chunk_size": 24})
    parts = list(split(text, sections, config, tokenizer))
    assert any(p.text == "Second paragraph." for p in parts)


def test_fixed_preserves_code_whitespace(tokenizer):
    text = "# Python\n\n```python\n" + "    if enabled:\n        call()\n" * 40 + "```"
    assert "".join(windows(text, tokenizer, 128)) == text


def test_validation_rejects_budget_and_wrong_overlap(settings):
    config = settings.preprocessing.model_dump()
    with pytest.raises(ValueError):
        PreprocessingConfig.model_validate({**config, "strategy": "fixed", "overlap": 0.1})
    with pytest.raises(ValueError):
        PreprocessingConfig.model_validate({**config, "chunk_size": 600})


def test_tokenizer_cache_and_digest(settings, tmp_path, tokenizer):
    from src.storage.files import digest

    data = tokenizer.backend.to_str().encode()
    config = settings.preprocessing.tokenizer.model_copy(update={"sha256": digest(data)})
    calls = []

    def response(request):
        calls.append(request)
        return httpx.Response(200, content=data)

    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        first = E5Tokenizer(config, tmp_path, client)
        second = E5Tokenizer(config, tmp_path, client)
    assert len(calls) == 1 and first.count("abc") == second.count("abc") == 3
    config = config.model_copy(update={"sha256": "a" * 64})
    with pytest.raises(AppError, match="SHA256"):
        E5Tokenizer(config, tmp_path)
