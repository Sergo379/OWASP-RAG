import json
import re

from src.core.config import AppError
from src.generation.evidence import annotated_text, clean_text, format_contract, registry

INJECTION = re.compile(
    r"(?i)(ignore (?:all |the |your )?(?:previous |system )?instructions|invent.{0,35}(?:recommendation|source)|reveal (?:the )?(?:system prompt|secrets)|игнорируй.{0,25}инструкци|<\|im_start\|>|\[INST\])"
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
    if re.search(
        r"(?i)(\bweather\b|\bforecast\b|погод[аыуе]|прогноз.{0,15}погод|(?:сейчас|сегодня|завтра).{0,25}температур|температур.{0,30}(?:Кишин|сейчас|сегодня))",
        query,
    ):
        return "out_of_domain"
    if re.search(
        r"(?i)(?:какой|назови|дай|what is|give me).{0,25}(?:пароль|password).{0,65}(?:мо[ейго]|частн|private|my project)",
        query,
    ):
        return "private_credential_not_in_corpus"
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


def prompt_text(text):
    # Preserve the words and whole chunk; destinations are available in the raw source UI.
    return clean_text(text)


def render_source(source, evidence_bound=False):
    return (
        "SOURCE ["
        + source["source_id"]
        + "]\n"
        + json.dumps(
            {
                "citation": "[" + source["source_id"] + "]",
                "title": source["title"],
                "section": source["section"],
                "text": annotated_text(source) if evidence_bound else prompt_text(source["original_text"]),
            },
            ensure_ascii=False,
        )
    )


def brief_requested(query):
    return bool(
        re.search(
            r"(?i)(\bbrief(?:ly)?\b|\bshort answer\b|one (?:short )?sentence|кратко|коротко|в одном предложении)",
            query,
        )
    )


def construct_prompt(sources, query, system, history, mode="strict_grounded", evidence_bound=False):
    language = "Russian (русский язык)" if russian(query) else "English"
    citations = ", ".join("[" + s["source_id"] + "]" for s in sources)
    if mode == "concise_grounded" or brief_requested(query):
        style = "Answer briefly; use 2 to 4 short points only when appropriate."
    elif mode == "technical_grounded":
        style = "Give a substantive engineering answer: one compact paragraph per distinct requested concern, with concrete parameters and rationale WHERE EVIDENCED. Combine overlapping concerns. Use a single verification checklist, not a repeated recommendation/parameters/purpose template per item. No fixed bullet/section count."
    else:
        style = "Adapt detail to the complexity of the question. Explain distinct supported recommendations without repetition; no fixed bullet count."
    if russian(query):
        if mode == "technical_grounded" and not brief_requested(query):
            style = (
                "Дай инженерный ответ по каждому запрошенному аспекту отдельным компактным пунктом: "
                "конкретное действие, подтверждённые параметры и назначение. Сохраняй условия и оговорки источника: "
                "рекомендация по умолчанию не равна запрету всех альтернатив. Ставь поддерживающую ссылку "
                "в конце соответствующего пункта, а не общим списком после всего ответа. Заверши одним чеклистом проверки. "
                "Число пунктов определяется вопросом. Не заменяй технические детали общим перечнем мер."
            )
        elif mode == "concise_grounded" or brief_requested(query):
            style = "Ответь кратко: несколько коротких пунктов или одно прямое объяснение."
        else:
            style = "Подбери глубину под вопрос. Дай конкретные подтверждённые рекомендации без повторов; число пунктов не ограничено. Сохраняй условия и оговорки источника."
    contract = (
        "\nEND OF EXCERPTS.\nAnswer the CURRENT QUESTION in "
        + language
        + ". "
        + style
        + " Every substantive paragraph/list item MUST cite supporting current IDs from: "
        + citations
        + ". If the answer is absent, output only INSUFFICIENT_CONTEXT. "
        "For partially supported questions identify the unsupported part explicitly. "
        "These public excerpts cannot establish private deployment statistics or credentials. "
        "Never give a private project's password, user count, or internal operational data.\n/no_think"
    )
    if russian(query):
        contract += (
            "\nТекущий вопрос: "
            + query
            + "\nОтветь по существу на русском. После каждого фактического абзаца или пункта поставь "
            "поддерживающую ссылку вида [S1] (латинская S), взятую из фрагментов выше. "
            "Начни с ответа, не повторяй вопрос. Не пиши отдельный пересказ того, что говорит каждый источник. "
            "Простой вопрос требует прямого короткого объяснения; инженерный — конкретных деталей. Не повторяй выводы.\n/no_think"
        )
    if evidence_bound:
        system += "\nFor the application's evidence-bound JSON contract, paragraph evidence_id replaces inline citations; the app renders source citations. All source-only/safety/qualification requirements still apply."
        contract += format_contract(sources, query, mode)
    return [
        {"role": "system", "content": system},
        *history,
        {
            "role": "user",
            "content": "CURRENT QUESTION:\n"
            + query
            + "\nUNTRUSTED OWASP EXCERPTS (JSON):\n"
            + "\n".join(render_source(s, evidence_bound) for s in sources)
            + contract,
        },
    ]


def count_messages(messages, tokenizer):
    # Qwen vocabulary count + per-message framing. The separate reserve covers
    # the Ollama Go template. Actual prompt_eval_count is reported after inference.
    return sum(tokenizer.count(m["content"]) + 8 for m in messages) + 8


def build_context(
    results, query, system, history, config, tokenizer, mode="strict_grounded", evidence_bound=False
):
    selected, excluded, used = [], [], 0
    feedback_reserve = (
        config.repair_feedback_tokens if evidence_bound and config.repair_on_validation_failure else 0
    )
    fixed = construct_prompt([], query, system, history, mode, evidence_bound)
    limit = min(
        config.context_tokens,
        config.num_ctx
        - config.num_predict
        - config.template_reserve_tokens
        - feedback_reserve
        - count_messages(fixed, tokenizer),
    )
    for candidate in results:
        source = {**candidate, "source_id": "S" + str(len(selected) + 1)}
        text = candidate["original_text"]
        cost = tokenizer.count(render_source(source, evidence_bound)) + 1
        reason = None
        if candidate.get("metadata", {}).get("heading_only") or not re.search(
            r"[\w]", re.sub(r"(?m)^\s*#+.*$", "", text)
        ):
            reason = "heading_only"
        elif INJECTION.search(text):
            reason = "instruction_like_content"
        elif used + cost > limit:
            reason = "context_token_budget"
        elif (
            count_messages(
                construct_prompt([*selected, source], query, system, history, mode, evidence_bound), tokenizer
            )
            + config.num_predict
            + config.template_reserve_tokens
            + feedback_reserve
            > config.num_ctx
        ):
            reason = "prompt_token_budget"
        if reason:
            excluded.append({"chunk_id": candidate["chunk_id"], "reason": reason, "tokens": cost})
        else:
            selected.append(source)
            used += cost
    messages = construct_prompt(selected, query, system, history, mode, evidence_bound)
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
            "repair_feedback_reserve_tokens": feedback_reserve,
            "excluded": excluded,
            "selected_chunk_ids": [s["chunk_id"] for s in selected],
            "truncation": "none; whole chunks only",
            "prompt_transformations": [
                "Markdown HTTP link destinations removed; labels and full chunk content retained; raw original available"
            ]
            + (
                ["Application paragraph IDs inserted; full source words retained, raw offsets preserved"]
                if evidence_bound
                else []
            ),
        },
    )


def validate_answer(answer, sources, query, request_id=None, *, evidence_bindings=None, evidence_errors=None):
    aliases = {}

    def canonical_citation(match):
        canonical = "[S" + match[1] + "]"
        aliases[match[0]] = canonical
        return canonical

    parts = re.split(r"(```.*?```|`[^`\n]*`)", answer, flags=re.S)
    answer = "".join(
        re.sub(r"\[[Сс](\d+)\]", canonical_citation, part) if i % 2 == 0 else part
        for i, part in enumerate(parts)
    )
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
    if len(allowed) != len(sources):
        reasons.append("ambiguous_source_ids")
    for source in sources:
        if source["source_id"] in used:
            if not source.get("original_text", "").strip():
                reasons.append("missing_source_text")
            if request_id and source.get("request_id", request_id) != request_id:
                reasons.append("source_request_mismatch")
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
            (len(block.strip()) > 20 or re.match(r"^\s*(?:[-*]|\d+\.)\s", block))
            and not re.search(r"\[S\d+\]", block)
            and not block.lstrip().startswith("#")
            and not (len(block.strip()) < 120 and re.fullmatch(r"\*\*[^*\n]+\*\*:?", block.strip()))
        ):
            if not re.search(
                r"(?i)(?:not (?:provided|established|covered) (?:in|by) (?:the |these )?(?:selected )?(?:excerpts|sources|context)|(?:в (?:выбранных|данных|этих) (?:фрагментах|источниках|документах).{0,60}(?:не (?:указан|содерж|установлен)|недостаточно)))",
                block,
            ):
                uncovered.append(block[:30])
    if uncovered:
        reasons.append("uncited_factual_blocks")
    from src.generation.grounding import check_claims, compact, literal_supported, literals

    grounding = check_claims(answer, sources)
    reasons.extend(grounding["failures"])
    evidence_issues = []
    if evidence_bindings is not None:
        chosen = registry(sources)
        lookup = {s["source_id"]: s for s in sources}
        exact = []
        if evidence_errors:
            reasons.append("invalid_evidence_structure")
        for index, binding in enumerate(evidence_bindings, 1):
            paragraph = chosen.get(binding.get("evidence_id"))
            start, end = binding.get("answer_start"), binding.get("answer_end")
            if (
                not paragraph
                or not isinstance(start, int)
                or not isinstance(end, int)
                or not 0 <= start < end <= len(answer)
            ):
                reasons.append("invalid_evidence_structure")
                continue
            source = lookup[paragraph["source_id"]]
            claim = answer[start:end]
            if set(re.findall(r"\[(S\d+)\]", claim)) != {paragraph["source_id"]}:
                reasons.append("invalid_evidence_structure")
            # The section heading is part of the delivered source context too
            # (e.g. an algorithm name followed by a paragraph saying "it").
            evidence_text = source.get("section", "") + "\n" + paragraph["quote"]
            narrowed = {**source, "original_text": evidence_text}
            local_failures = check_claims(claim, [narrowed])["failures"]
            reasons.extend(local_failures)
            unsupported_values = [
                value for value in literals(claim) if not literal_supported(value, evidence_text)
            ]
            unsupported_identifiers = []
            # Short inline technical identifiers must occur in the chosen paragraph.
            # Full code examples still require manual review; this is not a code/NLI judge.
            for identifier in re.findall(r"`([A-Za-z][A-Za-z0-9_-]*(?::\s*[A-Za-z][A-Za-z0-9_-]*)?)`", claim):
                if compact(identifier) not in compact(evidence_text):
                    reasons.append("identifier_not_in_selected_evidence")
                    unsupported_identifiers.append(identifier)
            if local_failures or unsupported_identifiers:
                evidence_issues.append(
                    {
                        "source_id": paragraph["source_id"],
                        "evidence_id": paragraph["evidence_id"],
                        "reasons": local_failures,
                        "unsupported_values": unsupported_values,
                        "unsupported_identifiers": unsupported_identifiers,
                    }
                )
            exact.append(
                {
                    **paragraph,
                    "claim_index": index,
                    "support_status": "model-selected exact source paragraph; semantic support unverified",
                }
            )
        grounding["evidence"] = exact
        grounding["checks"].append("current paragraph selection and per-statement literals; not entailment")
    reasons = list(dict.fromkeys(reasons))
    valid = not reasons
    if valid and re.search(r"(?i)private.{0,80}(?:password|credential|secret)", query):
        # Application-owned acknowledgement: public OWASP excerpts cannot supply
        # credentials of a named private deployment. This is not an LLM citation.
        answer += (
            "\n\nВ документации не представлены учётные данные частного приложения."
            if russian(query)
            else "\n\nThe public documentation does not establish the private application's credentials."
        )
    final = answer if valid else refusal(query)
    if not valid and "model_insufficient_context" not in reasons:
        final = (
            "Ответ локальной модели не прошёл проверку по выбранным источникам. Откройте диагностику и контекст; попробуйте уточнить вопрос или выбрать другую локальную модель."
            if russian(query)
            else "The local model's answer failed source validation. Inspect diagnostics and context; refine the question or try another local model."
        )
    return {
        "valid": valid,
        "used_source_ids": sorted(used & allowed),
        "unknown_source_ids": sorted(used - allowed),
        "reasons": reasons,
        "evidence": grounding["evidence"],
        "evidence_issues": evidence_issues,
        "protocol_errors": evidence_errors or [],
        "checks": grounding["checks"],
        "faithfulness": "not proven by citation-ID validation or lexical evidence alignment; human review required",
        "citation_aliases_normalized": aliases,
    }, final


def public_sources(sources, used_ids, evidence_bound=False):
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
        group["chunks"][-1]["prompt_text"] = (
            annotated_text(source) if evidence_bound else prompt_text(source["original_text"])
        )
    return list(groups.values())
