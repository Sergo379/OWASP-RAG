"""Export actual checkpoint rows, aggregations and paired diagnostics."""

import csv
import io
from collections import defaultdict
from statistics import mean

from src.storage.files import atomic_write, write_json

from .metrics import aggregate
from .reviews import review_for
from .storage import checked
from .visualization import generate_charts


def grouped(rows, manifest):
    configs = {}
    by_label = defaultdict(list)
    for row in rows:
        by_label[row["configuration_label"]].append(row)
    for conf in manifest["configurations"]:
        label = conf["label"]
        group = by_label[label]
        summary = aggregate(group)
        cohorts = {}
        for key in ("language", "category", "difficulty", "answerable"):
            categories = {str(row[key]) for row in group}
            cohorts[key] = {
                value: aggregate([r for r in group if str(r[key]) == value]) for value in sorted(categories)
            }
        reviews = [r["review"] for r in group if r.get("review")]
        review_metrics = {
            key: {"mean": mean(r["scores"][key] for r in reviews), "n": len(reviews)}
            for key in (reviews[0]["scores"] if reviews else {})
        }
        configs[label] = {
            **summary,
            "cohorts": cohorts,
            "family": conf["family"],
            "parameters": conf["parameters"],
            "planned": len(conf["questions"]),
            "reviewed": len(reviews),
            "review_metrics": review_metrics,
        }
    return configs


def paired(rows, left, right, metric=None):
    one = {r["question_id"]: r for r in rows if r["configuration_label"] == left}
    two = {r["question_id"]: r for r in rows if r["configuration_label"] == right}
    output = []
    for key in sorted(one.keys() & two.keys()):
        a, b = one[key], two[key]
        av = a.get("scores", {}).get(metric) if metric else None
        bv = b.get("scores", {}).get(metric) if metric else None
        output.append(
            {
                "question_id": key,
                "left_status": a["status"],
                "right_status": b["status"],
                "delta": bv - av if av is not None and bv is not None else None,
                "same_context": a["context_fingerprint"] == b.get("context_fingerprint")
                if a.get("context_fingerprint")
                else None,
                "latency_delta_ms": b["latency_ms"] - a["latency_ms"]
                if a.get("latency_ms") is not None and b.get("latency_ms") is not None
                else None,
            }
        )
    return output


def results(store, run_id):
    manifest = store.manifest(run_id)
    rows = []
    for key, row in store.rows(run_id).items():
        review = review_for(store, run_id, key, row)
        if review:
            row["review"] = review
            if row.get("scores", {}).get("refusal_outcome") == "answer_pending_review":
                row["scores"]["refusal_outcome"] = (
                    "correct_supported_answer"
                    if review["scores"]["correctness"] == 2 and review["scores"]["faithfulness"] == 2
                    else "reviewed_answer_with_issues"
                )
            row["scores"]["review_status"] = "REVIEWED"
        rows.append(row)
    labels = {row["configuration_label"] for row in rows}
    pairs = {}
    if {"rerank-off", "rerank-on"} <= labels:
        pairs["reranking_same_pool"] = [
            {
                "question_id": row["question_id"],
                "before": row["same_pool_without_rerank_scores"],
                "after": row["scores"],
                "rank_changes": row["rank_changes"],
            }
            for row in rows
            if row["configuration_label"] == "rerank-on" and row.get("same_pool_without_rerank_scores")
        ]
        pairs["reranking_latency"] = paired(rows, "rerank-off", "rerank-on", "mrr")
    for name, left, right in (("llm", "qwen-17", "qwen-4-instruct"), ("prompt", "qwen-17", "prompt-concise")):
        if {left, right} <= labels:
            pairs[name] = paired(rows, left, right)
    folder = store.path(run_id) / "indexes"
    return {
        "manifest": manifest,
        "state": store.state(run_id),
        "rows": rows,
        "pairs": pairs,
        "configurations": grouped(rows, manifest),
        "indexes": {p.stem: checked(p) for p in sorted(folder.glob("*.json"))},
    }


def export_csv(rows):
    fields = (
        "run_id",
        "experiment",
        "configuration_id",
        "configuration_label",
        "question_id",
        "split",
        "language",
        "category",
        "status",
        "started_at",
        "finished_at",
        "index_id",
        "index_generation",
        "model",
        "strategy",
        "size",
        "overlap",
        "k",
        "pool",
        "threshold",
        "rerank",
        "llm",
        "model_digest",
        "mode",
        "answer_format",
        "recall",
        "precision",
        "mrr",
        "hit_rate",
        "citation_validity",
        "refusal_outcome",
        "review_status",
        "latency_ms",
        "ttft_ms",
        "eval_count",
        "peak_combined_rss_bytes",
        "trace_id",
        "answer",
        "error",
    )
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    for row in rows:
        flat = {**row.get("parameters", {}), **row, **row.get("scores", {})}
        writer.writerow({key: safe_csv(flat.get(key)) for key in fields})
    return stream.getvalue().encode("utf-8-sig")


def safe_csv(value):
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def export_run(root, store, run_id, charts=False):
    report = results(store, run_id)
    output = root / "experiments/results" / run_id
    write_json(output / "results.json", report)
    atomic_write(output / "results.csv", export_csv(report["rows"]))
    filenames = []
    if charts:
        filenames = generate_charts(root / "experiments/charts" / run_id, report, report["manifest"])
    return {
        "run_id": run_id,
        "json": str(output / "results.json"),
        "csv": str(output / "results.csv"),
        "charts": filenames,
        "saved": len(report["rows"]),
        "state": report["state"]["status"],
    }
