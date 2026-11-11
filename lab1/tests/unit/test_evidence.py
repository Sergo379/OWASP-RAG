import json
import re

import httpx
import pytest

from src.core.config import ROOT
from src.generation.config import load_generation
from src.generation.context import build_context, validate_answer
from src.generation.evidence import EvidenceStream, annotated_text, clean_text, registry, response_schema
from src.generation.ollama import Cancellation, Ollama


def source(text="### Rules\n\nUse TLS 1.3 by default.\n\nTLS 1.2 may be used for compatibility.", sid="S1"):
    return {
        "source_id": sid,
        "chunk_id": sid * 32,
        "original_text": text,
        "section": "Rules",
        "title": "OWASP",
        "request_id": "a" * 32,
    }


def test_paragraph_markers_preserve_all_source_words_and_exact_unicode_offsets():
    src = source(
        "### Протоколы\n\nUse TLS 1.3; see [guidance](https://example.org/tls).\n\nПроверяйте конфигурацию."
    )
    annotated = annotated_text(src)
    assert re.sub(r"\[S\d+P\d+\]\n", "", annotated) == clean_text(src["original_text"])
    spans = registry([src])
    assert "S1P1" not in spans and len(spans) == 2
    for span in spans.values():
        assert src["original_text"][span["char_start"] : span["char_end"]] == span["quote"]


def test_schema_choices_are_current_source_scoped_and_language_explicit():
    schema = response_schema([source(), source(sid="S2")], "Какие версии TLS?", "technical_grounded")
    assert schema["required"] == ["S1", "S2", "missing_details"]
    options = schema["properties"]["S1"]["properties"]
    assert set(options["evidence_id"]["enum"]) == {"S1P2", "S1P3", "UNUSED"}
    assert "answer_ru" in options and "Russian" in options["answer_ru"]["description"]
    assert (
        "answer_en"
        in response_schema([source()], "TLS?", "strict_grounded")["properties"]["S1"]["properties"]
    )


def test_stream_emits_complete_human_blocks_before_root_closes_never_json():
    pieces = []
    stream = EvidenceStream([source()], "TLS?", pieces.append)
    stream.feed('{"S1":{"evidence_id":"S1P2","answer_en":"Use TLS ')
    assert not pieces
    stream.feed('1.3 by default."}')
    assert pieces == ["Use TLS 1.3 by default. [S1]"]
    stream.feed(',"missing_details":""}')
    answer, info = stream.finish()
    assert answer == pieces[0] and not info["evidence_errors"]
    binding = info["evidence_bindings"][0]
    assert answer[binding["answer_start"] : binding["answer_end"]] == pieces[0]
    assert "answer_en" not in answer and "quote" not in json.dumps(info)


def test_russian_answer_can_start_with_rest_api_while_english_source_copy_is_rejected():
    src = source("REST APIs must use HTTPS.")
    for text, valid in [("REST API должен использовать HTTPS.", True), ("REST APIs must use HTTPS.", False)]:
        stream = EvidenceStream([src], "Как защитить API?", lambda _: None)
        stream.feed(
            json.dumps(
                {"S1": {"evidence_id": "S1P1", "answer_ru": text}, "missing_details": None},
                ensure_ascii=False,
            )
        )
        _, info = stream.finish()
        assert bool(info["evidence_errors"]) is not valid


def test_full_prompt_reserves_room_for_conditional_repair_feedback(tokenizer):
    config, prompts = load_generation(ROOT)
    config = config.for_request("technical_grounded", "qwen3:1.7b", "Detailed TLS configuration").model_copy(
        update={"num_ctx": 8192}
    )
    rows = [source("Use TLS 1.3 by default. " * 25, sid="S" + str(i)) for i in range(1, 10)]
    chosen, _, info = build_context(
        rows,
        "Detailed TLS configuration",
        prompts["technical_grounded"],
        [],
        config,
        tokenizer,
        "technical_grounded",
        True,
    )
    assert chosen and info["excluded"] and info["repair_feedback_reserve_tokens"] == 384
    assert (
        info["estimated_prompt_tokens"] + config.num_predict + config.template_reserve_tokens + 384
        <= config.num_ctx
    )


@pytest.mark.parametrize(
    "raw",
    [
        '{"S1":{"evidence_id":"S2P2","answer_en":"Use TLS."},"missing_details":""}',
        '{"S1":{"evidence_id":"S1P2","answer_en":"Use TLS."}}',
        '{"S1":{"evidence_id":"S1P2","answer_en":"Use TLS.","answer_en":"Other text."},"missing_details":""}',
        '{"S1":{"evidence_id":"S1P2","answer_en":"Use TLS."}',
    ],
)
def test_invalid_schema_or_cross_source_choice_never_validates(raw):
    stream = EvidenceStream([source()], "TLS?", lambda _: None)
    for part in raw:
        stream.feed(part)
    answer, info = stream.finish()
    assert info["evidence_errors"]
    validation, _ = validate_answer(
        answer,
        [source()],
        "TLS?",
        "a" * 32,
        evidence_bindings=info["evidence_bindings"],
        evidence_errors=info["evidence_errors"],
    )
    assert not validation["valid"]


def test_model_cannot_borrow_version_from_another_paragraph_in_same_chunk():
    answer = "Use TLS 1.2 for compatibility [S1]."
    binding = [{"evidence_id": "S1P2", "answer_start": 0, "answer_end": len(answer)}]
    result, _ = validate_answer(answer, [source()], "TLS?", "a" * 32, evidence_bindings=binding)
    assert not result["valid"] and "technical_literal_not_in_cited_source" in result["reasons"]
    binding[0]["evidence_id"] = "S1P3"
    result, _ = validate_answer(answer, [source()], "TLS?", "a" * 32, evidence_bindings=binding)
    assert result["valid"] and result["evidence"][0]["quote"] == "TLS 1.2 may be used for compatibility."


def test_existing_id_and_selected_real_span_still_do_not_excuse_secure_error():
    src = source("Secure restricts cookie transmission to HTTPS.")
    answer = "Secure encrypts cookie values [S1]."
    result, _ = validate_answer(
        answer,
        [src],
        "Secure?",
        "a" * 32,
        evidence_bindings=[{"evidence_id": "S1P1", "answer_start": 0, "answer_end": len(answer)}],
    )
    assert "secure_attribute_is_not_cookie_encryption" in result["reasons"]


def test_unused_sources_produce_explicit_insufficient_context():
    stream = EvidenceStream([source()], "TLS?", lambda _: None)
    stream.feed('{"S1":{"evidence_id":"UNUSED","answer_en":""},"missing_details":""}')
    answer, info = stream.finish()
    assert answer == "INSUFFICIENT_CONTEXT" and info["unused_source_ids"] == ["S1"]


def test_real_adapter_payload_and_visible_stream_use_schema_without_changing_budgets():
    config, _ = load_generation(ROOT)
    captured, visible = [], []
    payload = '{"S1":{"evidence_id":"S1P2","answer_en":"Use TLS 1.3 by default."},"missing_details":""}'

    def transport(request):
        captured.append(request)
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "fixture"})
        if request.url.path == "/api/tags":
            return httpx.Response(
                200, json={"models": [{"name": k, "digest": v} for k, v in config.models.items()]}
            )
        if request.url.path == "/api/show":
            return httpx.Response(
                200, json={"template": "$.Think", "capabilities": ["completion", "thinking"]}
            )
        frames = [
            {"message": {"content": payload[:55]}},
            {"message": {"content": payload[55:]}},
            {"done": True, "done_reason": "stop", "eval_count": 40},
        ]
        return httpx.Response(200, content="\n".join(json.dumps(frame) for frame in frames))

    adapter = Ollama(config, httpx.MockTransport(transport))
    answer, metrics = adapter.stream(
        "qwen3:1.7b",
        [{"role": "user", "content": "TLS?"}],
        Cancellation(),
        visible.append,
        config=config.for_request("technical_grounded", "qwen3:1.7b", "TLS?"),
        evidence_sources=[source()],
        query="TLS?",
        mode="technical_grounded",
    )
    body = json.loads(captured[-1].content)
    assert body["format"]["properties"]["S1"]["properties"]["evidence_id"]["enum"] == [
        "S1P2",
        "S1P3",
        "UNUSED",
    ]
    assert body["options"]["num_ctx"] == 6144 and body["options"]["num_predict"] == 1536
    assert answer == "Use TLS 1.3 by default. [S1]" and visible == [answer]
    assert metrics["generation_protocol"] == "evidence_bound" and not metrics["evidence_errors"]
    assert metrics["ttft_ms"] >= metrics["raw_ttft_ms"]
