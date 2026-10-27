import json
import re

from src.core.config import AppError

INJECTION = re.compile(
    r"(?i)(ignore (?:all |the )?(?:previous|system) instructions|reveal (?:the )?(?:system prompt|secrets)|игнорируй.{0,25}инструкци|<\|im_start\|>|\[INST\])"
)


def russian(query):
    return bool(re.search(r"[А-Яа-яЁё]", query))


def refusal(query):
    return (
        "В выбранных фрагментах OWASP недостаточно сведений для ответа на этот вопрос. Уточните вопрос или параметры поиска."
        if russian(query)
        else "The selected OWASP excerpts do not provide enough evidence to answer this question. Refine the question or retrieval settings."
    )


def adequacy(query, results):
    if INJECTION.search(query):
        return "instruction_override"
    if re.search(r"(?i)(\bweather\b|\bforecast\b|погод[аыуе]|прогноз.{0,15}погод)", query):
        return "out_of_domain"
    if not results:
        return "no_eligible_results"
    # A high semantic score cannot establish facts about a named private system.
    # Anchor explicit project names to source text before letting the model infer.
    text = "\n".join(s["original_text"] for s in results).lower()
    named = re.findall(r"\b[A-Z][A-Za-z0-9]+(?:[-_][A-Z][A-Za-z0-9]+)+\b", query)
    generic_security_part = bool(
        re.search(
            r"(?i)(how (?:can|should|to).{0,55}(?:password|authenticat|session|sql|secur|protect)|как.{0,45}(?:хранить|защит|безопас|парол))",
            query,
        )
    )
    if any(name.lower() not in text for name in named) and not generic_security_part:
        return "named_entity_not_in_context"
    if (
        re.search(
            r"(?i)(exact (?:number|count)|точное (?:число|количество)).{0,80}(?:active users|production|пользовател)",
            query,
        )
        and not generic_security_part
    ):
        return "operational_fact_not_in_corpus"
    # Similarity alone is not evidence of answerability. Final LLM sentinel and
    # citation validation are additional gates, not a guarantee of faithfulness.
    return None


def history_messages(messages, config, tokenizer):
    pairs = []
    for i in range(0, len(messages) - 1, 2):
        user, assistant = messages[i : i + 2]
        if (
            user.get("role") == "user"
            and assistant.get("role") == "assistant"
            and assistant.get("status") == "completed"
        ):
            pairs.append((user, assistant))
    selected, used = [], 0
    for pair in reversed(pairs[-config.history_turns :] if config.history_turns else []):
        cleaned = [{"role": m["role"], "content": re.sub(r"\[S\d+\]", "", m["content"])} for m in pair]
        cost = sum(tokenizer.count(m["content"]) + 8 for m in cleaned)
        if used + cost > config.history_tokens:
            break
        selected = cleaned + selected
        used += cost
    return selected, {
        "history_messages": len(selected),
        "history_tokens": used,
        "omitted_messages": len(messages) - len(selected),
    }


def render_source(source):
    return json.dumps(
        {
            "citation": "[" + source["source_id"] + "]",
            "title": source["title"],
            "section": source["section"],
            "text": source["original_text"],
        },
        ensure_ascii=False,
    )


def construct_prompt(sources, query, system, history):
    language = "Russian (русский язык)" if russian(query) else "English"
    citations = ", ".join("[" + s["source_id"] + "]" for s in sources)
    contract = (
        "\nEND OF EXCERPTS.\nAnswer the CURRENT QUESTION in "
        + language
        + ". Use 2 to 4 short bullets, with no introduction or conclusion. Each bullet MUST end "
        "with a relevant exact citation from: "
        + citations
        + ". If the answer is absent, output only INSUFFICIENT_CONTEXT. "
        "For partially supported questions identify the unsupported part explicitly. "
        "These public excerpts cannot establish private deployment statistics or credentials. "
        "Never give a private project's password, user count, or internal operational data.\n/no_think"
    )
    return [
        {"role": "system", "content": system},
        *history,
        {
            "role": "user",
            "content": "CURRENT QUESTION:\n"
            + query
            + "\nUNTRUSTED OWASP EXCERPTS (JSON):\n"
            + "\n".join(render_source(s) for s in sources)
            + contract,
        },
    ]


def count_messages(messages, tokenizer):
    # Qwen vocabulary count + per-message framing. The separate reserve covers
    # the Ollama Go template. Actual prompt_eval_count is reported after inference.
    return sum(tokenizer.count(m["content"]) + 8 for m in messages) + 8


def build_context(results, query, system, history, config, tokenizer):
    selected, excluded, used = [], [], 0
    fixed = construct_prompt([], query, system, history)
    limit = min(
        config.context_tokens,
        config.num_ctx
        - config.num_predict
        - config.template_reserve_tokens
        - count_messages(fixed, tokenizer),
    )
    for candidate in results:
        source = {**candidate, "source_id": "S" + str(len(selected) + 1)}
        text = candidate["original_text"]
        cost = tokenizer.count(render_source(source)) + 1
        reason = None
        if candidate.get("metadata", {}).get("heading_only") or not re.search(
            r"[\w]", re.sub(r"(?m)^\s*#+.*$", "", text)
        ):
            reason = "heading_only"
        elif INJECTION.search(text):
            reason = "instruction_like_content"
        elif used + cost > limit:
            reason = "context_token_budget"
        if reason:
            excluded.append({"chunk_id": candidate["chunk_id"], "reason": reason, "tokens": cost})
        else:
            selected.append(source)
            used += cost
    messages = construct_prompt(selected, query, system, history)
    estimated = count_messages(messages, tokenizer)
    if estimated + config.num_predict + config.template_reserve_tokens > config.num_ctx:
        raise AppError("Вопрос и system prompt превышают бюджет контекста; сократите вопрос")
    return (
        selected,
        messages,
        {
            "context_tokens": used,
            "context_token_limit": max(0, limit),
            "estimated_prompt_tokens": estimated,
            "tokenizer_identity": tokenizer.identity,
            "template_reserve_tokens": config.template_reserve_tokens,
            "excluded": excluded,
            "selected_chunk_ids": [s["chunk_id"] for s in selected],
            "truncation": "none; whole chunks only",
        },
    )


def validate_answer(answer, sources, query):
    allowed = {s["source_id"] for s in sources}
    used = set(re.findall(r"\[(S\d+)\]", answer))
    reasons = []
    if "INSUFFICIENT_CONTEXT" in answer:
        reasons.append("model_insufficient_context")
    if not answer.strip():
        reasons.append("empty_answer")
    if used - allowed:
        reasons.append("unknown_citations")
    if not used:
        reasons.append("missing_citations")
    if re.search(r"https?://|www\.", answer, re.I):
        reasons.append("generated_url")
    if re.search(r"</?think|<\|im_", answer, re.I):
        reasons.append("reasoning_or_control_tag")
    if (
        russian(query)
        and len(re.findall(r"[А-Яа-яЁё]", answer)) < 10
        and "model_insufficient_context" not in reasons
    ):
        reasons.append("answer_language_mismatch")
    # Factual blocks need their own citations; code stays attached to its prose.
    without_code = re.sub(r"```.*?```", "[code]", answer, flags=re.S)
    uncovered = []
    for block in re.split(r"\n\s*\n|(?m:^\s*(?=[-*] |\d+\. ))", without_code):
        if (
            (len(block.strip()) > 40 or re.match(r"^\s*(?:[-*]|\d+\.)\s", block))
            and not re.search(r"\[S\d+\]", block)
            and not block.lstrip().startswith("#")
        ):
            if not re.search(
                r"(?i)(not (?:provided|established|covered)|недостаточно|не (?:указан|содерж|установлен))",
                block,
            ):
                uncovered.append(block[:30])
    if uncovered:
        reasons.append("uncited_factual_blocks")
    valid = not reasons
    if valid and re.search(r"(?i)private.{0,80}(?:password|credential|secret)", query):
        # Application-owned acknowledgement: public OWASP excerpts cannot supply
        # credentials of a named private deployment. This is not an LLM citation.
        answer += (
            "\n\nВ документации не представлены учётные данные частного приложения."
            if russian(query)
            else "\n\nThe public documentation does not establish the private application's credentials."
        )
    return {
        "valid": valid,
        "used_source_ids": sorted(used & allowed),
        "unknown_source_ids": sorted(used - allowed),
        "reasons": reasons,
        "faithfulness": "not proven by citation-ID validation",
    }, answer if valid else refusal(query)


def public_sources(sources, used_ids):
    groups = {}
    for source in sources:
        if source["source_id"] not in used_ids:
            continue
        group = groups.setdefault(
            source["document_id"],
            {
                "document_id": source["document_id"],
                "title": source["title"],
                "url": source["url"],
                "chunks": [],
            },
        )
        group["chunks"].append(
            {
                k: source[k]
                for k in (
                    "source_id",
                    "chunk_id",
                    "section",
                    "vector_similarity",
                    "rerank_score",
                    "original_text",
                )
            }
        )
    return list(groups.values())
