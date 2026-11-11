"""Ground truth at document level, plus separate evidence and operational metrics."""

from collections import Counter
from statistics import mean

QUALITY = ("correctness", "faithfulness", "completeness", "citation_support", "technical_detail")
MAX_SCORE = {key: (3 if key == "technical_detail" else 2) for key in QUALITY}


def ranking(rows, relevant, k):
    docs = [row["document_id"] for row in rows[:k]]
    gold = set(relevant)
    counts = {
        "returned": len(docs),
        "repeated_documents": len(docs) - len(set(docs)),
        "relevant_chunk_share": sum(d in gold for d in docs) / len(docs) if docs and gold else None,
    }
    if not gold:
        return {**counts, **dict.fromkeys(("recall", "precision", "mrr", "hit_rate"))}
    hits = {d for d in docs if d in gold}
    first = next((rank for rank, d in enumerate(docs, 1) if d in gold), None)
    return {
        **counts,
        "recall": len(hits) / len(gold),
        "precision": len(hits) / k,
        "mrr": 1 / first if first else 0.0,
        "hit_rate": float(bool(hits)),
    }


def source_coverage(rows, question):
    evidence = question["source_evidence"]
    if not evidence:
        return None
    supported = {
        e["claim_id"]
        for e in evidence
        if any(
            row["document_id"] == e["document_id"]
            and e["anchor"].casefold() in (row.get("original_text") or row.get("text") or "").casefold()
            for row in rows
        )
    }
    return len(supported) / len(question["expected_claims"])


def generation_scores(result, question):
    status = result["status"]
    validation = result["diagnostics"].get("validation") or {}
    if status not in ("completed", "refused"):
        return {
            "refusal_outcome": None,
            "refusal_accuracy": None,
            "citation_validity": None,
            "review_status": "NOT_EVALUATED",
        }
    if status == "refused":
        outcome = "false_refusal" if question["answerable"] else "correct_refusal"
    else:
        outcome = "answer_pending_review" if question["answerable"] else "unsupported_answer"
    return {
        "refusal_outcome": outcome,
        "refusal_accuracy": float((status == "refused") != question["answerable"]),
        "citation_validity": float(
            bool(validation.get("used_source_ids")) and not validation.get("unknown_source_ids")
        )
        if status == "completed"
        else None,
        "review_status": "NOT_REVIEWED" if status == "completed" else "NOT_APPLICABLE",
    }


def percentile(values, q):
    ordered = sorted(values)
    if not ordered:
        return None
    rank = (len(ordered) - 1) * q
    lo = int(rank)
    return ordered[lo] + (ordered[min(lo + 1, len(ordered) - 1)] - ordered[lo]) * (rank - lo)


def aggregate(rows):
    metrics = {}
    fields = {key for row in rows for key in row.get("scores", {})}
    for field in sorted(fields):
        values = [row["scores"].get(field) for row in rows]
        numbers = [v for v in values if type(v) in (float, int)]
        if numbers:
            metrics[field] = {"mean": mean(numbers), "n": len(numbers)}
    for field in (
        "latency_ms",
        "cpu_seconds",
        "peak_combined_rss_bytes",
        "peak_python_rss_bytes",
        "peak_all_ollama_rss_bytes",
        "eval_count",
        "ttft_ms",
    ):
        numbers = [v for row in rows if (v := row.get(field)) is not None]
        if numbers:
            metrics[field] = {
                "mean": mean(numbers),
                "median": percentile(numbers, 0.5),
                "p95": percentile(numbers, 0.95),
                "max": max(numbers),
                "n": len(numbers),
            }
    return {
        "n": len(rows),
        "status": dict(Counter(row["status"] for row in rows)),
        "refusal_outcomes": dict(
            Counter(
                row.get("scores", {}).get("refusal_outcome")
                for row in rows
                if row.get("scores", {}).get("refusal_outcome")
            )
        ),
        "metrics": metrics,
    }
