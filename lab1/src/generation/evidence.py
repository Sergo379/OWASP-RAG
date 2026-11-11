"""Single-call source slots with application-owned, inspectable paragraph spans.

The schema constrains provenance and repetition, not semantic entailment.
No model-generated quotation is treated as evidence.
"""

import json
import re


def clean_text(text):
    return re.sub(r"\[([^\]]+)\]\(https?://[^)]+\)", r"\1", text)


def paragraphs(source):
    result = []
    for number, match in enumerate(
        re.finditer(r"\S(?:.*?\S)?(?=\n\s*\n|\Z)", source["original_text"], flags=re.S), 1
    ):
        text = match[0]
        eligible = len(text.strip()) > 10 and not all(
            line.lstrip().startswith("#") for line in text.splitlines()
        )
        result.append(
            {
                "evidence_id": source["source_id"] + "P" + str(number) if eligible else None,
                "source_id": source["source_id"],
                "chunk_id": source["chunk_id"],
                "char_start": match.start(),
                "char_end": match.end(),
                "quote": text,
            }
        )
    return result


def registry(sources):
    return {p["evidence_id"]: p for source in sources for p in paragraphs(source) if p["evidence_id"]}


def annotated_text(source):
    parts, cursor = [], 0
    for paragraph in paragraphs(source):
        parts.append(clean_text(source["original_text"][cursor : paragraph["char_start"]]))
        if paragraph["evidence_id"]:
            parts.append("[" + paragraph["evidence_id"] + "]\n")
        parts.append(clean_text(paragraph["quote"]))
        cursor = paragraph["char_end"]
    parts.append(clean_text(source["original_text"][cursor:]))
    return "".join(parts)


def field_name(query):
    return "answer_ru" if re.search(r"[А-Яа-яЁё]", query) else "answer_en"


def response_schema(sources, query, mode):
    evidence = registry(sources)
    name = field_name(query)
    # Generous per-field cap; the global num_predict remains the CPU/output bound.
    cap = {"concise_grounded": 350, "strict_grounded": 800, "technical_grounded": 1600}[mode]
    properties = {}
    for source in sources:
        sid = source["source_id"]
        properties[sid] = {
            "type": "object",
            "properties": {
                "evidence_id": {
                    "type": "string",
                    "enum": [key for key, p in evidence.items() if p["source_id"] == sid] + ["UNUSED"],
                },
                name: {
                    "type": "string",
                    "pattern": r"^[\s\S]{0," + str(cap) + "}$",
                    "description": "Russian recommendation, not an English source copy"
                    if name == "answer_ru"
                    else "English recommendation",
                },
            },
            "required": ["evidence_id", name],
            "additionalProperties": False,
        }
    properties["missing_details"] = {"type": ["string", "null"], "maxLength": 400}
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def format_contract(sources, query, mode):
    schema = response_schema(sources, query, mode)
    if field_name(query) == "answer_ru":
        instruction = (
            "Верни JSON по схеме ниже, ровно один слот на текущий источник. Сначала выбери evidence_id его "
            "конкретного поддерживающего абзаца, затем answer_ru: самостоятельную рекомендацию на русском, "
            "непосредственно подтверждаемую этим абзацем. Сохрани условия и ограничения. Не копируй английский текст. "
            "Не добавляй внешние алгоритмы, параметры, инструменты или причинные выводы. UNUSED и пустой ответ означают, "
            "что источник не даёт отдельной полезной рекомендации или повторяет уже сказанное. "
            "Не пиши цитаты и ссылки: приложение добавит их из выбранного абзаца. Без вступления и итогового повторения. "
            "missing_details: только запрошенные аспекты, которые не установлены контекстом; иначе null. "
            "На вопрос да/нет начни с прямого ответа. В technical объясни действия, поддержанные параметры и назначение; "
            "в concise выбирай только главные короткие рекомендации."
        )
    else:
        instruction = (
            "Return JSON matching the schema below, one slot per current source. Choose a supporting paragraph's "
            "evidence_id BEFORE writing answer_en: an English recommendation directly supported by it, preserving "
            "conditions and limits. No external parameters, algorithms, tools or causal claims. Use UNUSED and empty "
            "answer for irrelevant/redundant sources. No quotations or inline citations: the app renders provenance. "
            "No introduction or repeated summary. missing_details names only requested aspects not established by "
            "the context, otherwise null. Start yes/no questions directly. Technical mode needs actions, supported "
            "parameters and purpose; concise mode only the main short recommendations."
        )
    return (
        "\nAPPLICATION OUTPUT CONTRACT:\n"
        + instruction
        + "\n"
        + json.dumps(schema, ensure_ascii=False)
        + "\n/no_think"
    )


def render_statement(text, source_id):
    """Keep model Markdown/code, attach the selected source outside fenced code."""
    lines, in_code = [], False
    for line in text.strip().splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
            lines.append(line)
            if not in_code:
                lines.append("[" + source_id + "]")
        elif not line.strip() or in_code or line.lstrip().startswith("#"):
            lines.append(line)
        else:
            lines.append(line + " [" + source_id + "]")
    return "\n".join(lines), in_code


class EvidenceStream:
    """Emit complete source slots as provisional paragraphs, never raw JSON."""

    def __init__(self, sources, query, emit, mode="strict_grounded"):
        self.sources = {s["source_id"]: s for s in sources}
        self.evidence = registry(sources)
        self.name = field_name(query)
        self.emit = emit
        self.raw, self.answer = "", ""
        self.cursor, self.opened = 0, False
        self.seen, self.bindings, self.errors, self.unused = set(), [], [], []
        self.missing = ""
        self.cap = {"concise_grounded": 350, "strict_grounded": 800, "technical_grounded": 1600}[mode]

    def accept(self, key, value):
        if not isinstance(key, str):
            self.errors.append("invalid_source_key")
            return
        if key in self.seen:
            self.errors.append("duplicate_source_slot")
            return
        self.seen.add(key)
        if key == "missing_details":
            if value is None:
                self.missing = ""
            elif isinstance(value, str):
                self.missing = value.strip()
                if len(value) > 400:
                    self.errors.append("invalid_missing_details_bounds")
            else:
                self.errors.append("invalid_missing_details")
            return
        if key not in self.sources or not isinstance(value, dict) or set(value) != {"evidence_id", self.name}:
            self.errors.append("invalid_source_slot")
            return
        eid, text = value["evidence_id"], value[self.name]
        if not isinstance(text, str):
            self.errors.append("invalid_statement_text")
            return
        if eid == "UNUSED":
            self.unused.append(key)
            return
        if not isinstance(eid, str) or eid not in self.evidence or self.evidence[eid]["source_id"] != key:
            self.errors.append("unknown_or_cross_source_evidence")
            return
        if not text.strip():
            self.errors.append("empty_statement")
            return
        initial = r"[А-Яа-яЁё]" if self.name == "answer_ru" else r"[A-Za-z]"
        if not re.search(initial, text):
            self.errors.append(key + ":invalid_statement_language")
        if len(text) > self.cap or (len(text) == self.cap and text[-1].isalnum()):
            self.errors.append(key + ":statement_character_bound")
        rendered, unclosed = render_statement(text, key)
        if unclosed:
            self.errors.append(key + ":unclosed_code_fence")
        prefix = "\n\n" if self.answer else ""
        start = len(self.answer) + len(prefix)
        self.answer += prefix + rendered
        self.bindings.append({"evidence_id": eid, "answer_start": start, "answer_end": len(self.answer)})
        self.emit(prefix + rendered)

    def feed(self, part):
        self.raw += part
        if not self.opened:
            stripped = self.raw.lstrip()
            if not stripped.startswith("{"):
                return
            self.cursor = self.raw.index("{") + 1
            self.opened = True
        decoder = json.JSONDecoder()
        while True:
            cursor = self.cursor
            while cursor < len(self.raw) and self.raw[cursor] in " \t\r\n,":
                cursor += 1
            if cursor >= len(self.raw) or self.raw[cursor] == "}":
                return
            try:
                key, end = decoder.raw_decode(self.raw, cursor)
                colon = end
                while colon < len(self.raw) and self.raw[colon].isspace():
                    colon += 1
                if colon >= len(self.raw) or self.raw[colon] != ":":
                    return
                cursor = colon + 1
                while cursor < len(self.raw) and self.raw[cursor].isspace():
                    cursor += 1
                value, end = decoder.raw_decode(self.raw, cursor)
            except (json.JSONDecodeError, ValueError):
                return
            self.cursor = end
            self.accept(key, value)

    def finish(self):
        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate JSON key")
                result[key] = value
            return result

        try:
            value = json.loads(self.raw, object_pairs_hook=unique_object)
            if not isinstance(value, dict) or set(value) != set(self.sources) | {"missing_details"}:
                self.errors.append("missing_or_extra_source_slots")
        except (json.JSONDecodeError, ValueError):
            self.errors.append("invalid_structured_json")
        if self.missing and self.bindings:
            prefix = (
                "В выбранных фрагментах не указано: "
                if self.name == "answer_ru"
                else "Not established in the selected excerpts: "
            )
            self.answer += "\n\n" + prefix + self.missing
        if not self.bindings and not self.errors:
            self.answer = "INSUFFICIENT_CONTEXT"
        return self.answer, {
            "generation_protocol": "evidence_bound",
            "evidence_bindings": self.bindings,
            "evidence_errors": sorted(set(self.errors)),
            "unused_source_ids": self.unused,
            "stream_unit": "complete provisional source block",
            "schema_source_slots": len(self.sources),
        }
