"""Cheap, deliberately limited claim checks and inspectable evidence spans.

Literal/numeric anchoring and known-error guards are NOT semantic entailment.
"""

import re

from src.retrieval.lexical import terms

UNITS = r"MiB|GiB|KiB|bits?|bytes?|iterations?|seconds?|days?|секунд\w*|дней|день|дня|бит(?:а|ов)?|байт\w*|итераци\w*"


def blocks(answer):
    text = re.sub(r"```.*?```", "[code]", answer, flags=re.S)
    return [
        b.strip()
        for b in re.split(r"\n\s*\n|(?m:^\s*(?=[-*] |\d+\. ))", text)
        if b.strip() and not b.lstrip().startswith("#")
    ]


def literals(text):
    return re.findall(
        r"(?i)\b(?:TLS|SSL)\s*v?\d+(?:\.\d+)+\b|\bTLS_[A-Z0-9_]+\b|\b\d+(?:[.,]\d+)*\s*(?:"
        + UNITS
        + r")\b|\bmax-age\s*=\s*\d+",
        text,
    )


def compact(text):
    return re.sub(r"[\s`*_,-]", "", text).casefold()


def literal_supported(value, text):
    if compact(value) in compact(text):
        return True
    quantity = re.fullmatch(r"(?i)(\d+(?:[.,]\d+)*)\s*(" + UNITS + ")", value)
    if quantity:

        def normalized_quantity(match):
            unit = match[2].casefold()
            for pattern, canonical in (
                (r"seconds?|секунд\w*", "second"),
                (r"days?|дней|день|дня", "day"),
                (r"bits?|бит(?:а|ов)?", "bit"),
                (r"bytes?|байт\w*", "byte"),
                (r"iterations?|итераци\w*", "iteration"),
            ):
                if re.fullmatch(pattern, unit):
                    unit = canonical
                    break
            number = match[1]
            number = (
                number.replace(",", "")
                if re.fullmatch(r"\d{1,3}(?:,\d{3})+", number)
                else number.replace(",", ".")
            )
            return (number, unit)

        return any(
            normalized_quantity(quantity) == normalized_quantity(candidate)
            for candidate in re.finditer(r"(?i)\b(\d+(?:[.,]\d+)*)\s*(" + UNITS + r")\b", text)
        )
    protocol = re.fullmatch(r"(?i)(TLS|SSL)\s*v?(\d+(?:\.\d+)+)", value)
    if not protocol:
        return False
    # Sources legitimately share a prefix: "TLS versions 1.0 and 1.1".
    # Resolve only an adjacent numeric list of the same protocol, not arbitrary
    # version-like numbers elsewhere in a cited document.
    for group in re.finditer(
        r"(?i)\b(TLS|SSL)(?:\s+versions?)?\s*v?[\s`*]*\d+(?:\.\d+)+(?:[\s`*,/]*(?:(?:and|or)\s+)?v?[\s`*]*\d+(?:\.\d+)+)*",
        text,
    ):
        if group[1].casefold() == protocol[1].casefold() and protocol[2] in re.findall(
            r"\d+(?:\.\d+)+", group[0]
        ):
            return True
    return False


def secure_confusion(text):
    clean = re.sub(r"[`*]", "", text)
    for sentence in re.split(r"\n|;|(?<!\d)[.!?](?!\d)", clean):
        if not re.search(r"\bSecure\b", sentence, re.I):
            continue
        # Check individual clauses: an explicitly negated encryption verb is safe.
        for clause in re.split(r",\s*(?=но\b|but\b)|\b(?:however|однако)\b", sentence, flags=re.I):
            if not re.search(r"\bSecure\b", clause, re.I):
                continue
            if re.search(
                r"(?i)(?:не|not|cannot|doesn't|isn't|without)\W+(?:\w+\W+){0,4}(?:шифр\w*|encrypt\w*)", clause
            ):
                continue
            required_value = re.search(
                r"(?i)\bSecure\b.{0,65}(?:требу\w*|обязыв\w*|requires?|demands?).{0,30}(?:шифр\w*|encrypt\w*).{0,35}(?:содержим\w*|значен\w*|values?|contents?)",
                clause,
            )
            required_value_reverse = re.search(
                r"(?i)\bSecure\b.{0,65}(?:требу\w*|requires?).{0,30}(?:содержим\w*|значен\w*|values?|contents?).{0,30}(?:шифр\w*|encrypt\w*)",
                clause,
            )
            if any(
                match and not re.search(r"(?i)HTTPS|TLS", match[0])
                for match in (required_value, required_value_reverse)
            ):
                return True
            direct = re.search(
                r"(?i)\bSecure\b.{0,45}(?:шифру\w*|осуществляет шифр\w*|encrypts?|обеспечивает шифр\w*)",
                clause,
            )
            reverse = re.search(
                r"(?i)(?:шифр\w*|encryption|encrypt\w*).{0,65}(?:с помощью|посредством|using|via|through).{0,25}\bSecure\b",
                clause,
            )
            if reverse or (direct and not re.search(r"(?i)HTTPS|TLS", direct.group())):
                return True
    return False


def check_claims(answer, sources):
    lookup = {s["source_id"]: s for s in sources}
    failures, evidence = [], []
    if secure_confusion(answer):
        failures.append("secure_attribute_is_not_cookie_encryption")
    for index, block in enumerate(blocks(answer), 1):
        ids = set(re.findall(r"\[(S\d+)\]", block))
        cited = [lookup[s] for s in sorted(ids & lookup.keys())]
        text = "\n".join(s.get("original_text", "") for s in cited)
        unsupported = [value for value in literals(block) if not literal_supported(value, text)]
        if cited and unsupported:
            failures.append("technical_literal_not_in_cited_source")
        # Exact quoted text is checkable; don't mistake Markdown emphasis/backticks for a quotation.
        for quote in re.findall(r'«([^»]{15,400})»|“([^”]{15,400})”|"([^"\n]{25,400})"', block):
            quoted = next(part for part in quote if part)
            # Quoting one technical token (e.g. «man-in-the-middle») labels a term;
            # it does not assert a verbatim sentence from OWASP.
            if not re.search(r"\s", quoted):
                continue
            if cited and quoted not in text:
                failures.append("quotation_not_in_cited_source")
        query_terms = set(terms(re.sub(r"\[S\d+\]", "", block)))
        for source in cited:
            original = source.get("original_text", "")
            spans = []
            # Whole source paragraphs preserve decimal versions and Markdown URLs;
            # splitting on every dot produced misleading fragments such as "3".
            for match in re.finditer(r"\S(?:.*?\S)?(?=\n\s*\n|\Z)", original, flags=re.S):
                fragment = match.group().strip()
                if len(fragment) < 20 or all(line.lstrip().startswith("#") for line in fragment.splitlines()):
                    continue
                overlap = len(query_terms & set(terms(fragment)))
                spans.append((overlap, match.start(), match.end()))
            for overlap, start, end in sorted(spans, key=lambda x: (-x[0], x[1]))[:1]:
                evidence.append(
                    {
                        "claim_index": index,
                        "source_id": source["source_id"],
                        "chunk_id": source["chunk_id"],
                        "char_start": start,
                        "char_end": end,
                        "quote": original[start:end],
                        "matched_terms": overlap,
                        "support_status": "literal span; semantic support unverified",
                    }
                )
    return {
        "failures": sorted(set(failures)),
        "evidence": evidence,
        "checks": [
            "current-source IDs and text",
            "known Secure/cookie confusion",
            "cited technical literals",
            "exact quotations",
            "lexical evidence spans; not entailment",
        ],
    }
