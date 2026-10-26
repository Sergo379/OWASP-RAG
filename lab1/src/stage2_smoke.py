"""Separate real-weight acceptance smoke. Never runs as part of unit pytest."""

import argparse
import importlib.metadata
import json
import platform
import socket
import threading
import time

import numpy as np
import psutil

from src.core.config import AppError, Settings
from src.core.service import Service
from src.embeddings.provider import validate_vectors
from src.observability.local import configure
from src.retrieval.models import SearchRequest
from src.storage.files import now, write_json


def main():
    parser = argparse.ArgumentParser(description="Real local models + persistent Chroma smoke")
    parser.add_argument("--demos", action="store_true", help="All 10 RU/EN queries for each embedding model")
    parser.add_argument(
        "--offline", action="store_true", help="Block non-loopback socket connections in this process"
    )
    parser.add_argument(
        "--export", help="Optional compact JSON results path, e.g. docs/stage2-demo-results.json"
    )
    args = parser.parse_args()
    settings = Settings()
    configure(settings.root)
    if args.offline:
        original_connect = socket.socket.connect

        def local_only(sock, address):
            if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
                return original_connect(sock, address)
            raise AssertionError("Offline real-model smoke attempted an external network connection")

        socket.socket.connect = local_only
    service = Service(settings)
    demo_queries = json.loads((settings.root / "configs/demo_queries.json").read_text(encoding="utf-8"))
    queries = demo_queries if args.demos else demo_queries[:2]
    peak_rss = [psutil.Process().memory_info().rss]
    stop_sampling = threading.Event()

    def sample_rss():
        while not stop_sampling.wait(0.05):
            peak_rss[0] = max(peak_rss[0], psutil.Process().memory_info().rss)

    sampler = threading.Thread(target=sample_rss, daemon=True)
    sampler.start()
    report = {
        "tested_at": now(),
        "platform": platform.platform(),
        "processor": platform.processor(),
        "logical_cpus": psutil.cpu_count(),
        "ram_total_bytes": psutil.virtual_memory().total,
        "offline_process_network_block": args.offline,
        "physical_internet_disconnect": "NOT TESTED by this command",
        "dependencies": {
            name: importlib.metadata.version(name)
            for name in ("torch", "transformers", "chromadb", "numpy", "tokenizers", "fastapi")
        },
        "models": {},
        "queries": [],
        "checks": {},
    }
    report.update(
        batch_size=service.retrieval_config.batch_size, cpu_threads=service.retrieval_config.cpu_threads
    )
    try:
        records = service.vectors.lists(service.operation())
        for model in ("e5", "minilm"):
            matching = [r for r in records if r["searchable"] and r["identity"]["model"] == model]
            if not matching:
                raise AppError(
                    f"Нет готового {model} индекса. Выполните pixi run build-index --model {model}"
                )
            # Prefer section include_all for a repeatable comparison, then use another ready index.
            record = next(
                (
                    r
                    for r in matching
                    if r["identity"]["preprocessing_configuration"]["strategy"] == "section"
                    and r["identity"]["embedding_configuration"]["heading_policy"] == "include_all"
                ),
                matching[0],
            )
            index_id = record["index_id"]
            before_switch_rss = psutil.Process().memory_info().rss
            start = time.perf_counter()
            provider = service.models.get(model)
            load_ms = (time.perf_counter() - start) * 1000
            first = provider.embed_documents(["Use parameterized SQL queries."])
            second = provider.embed_documents(["Use parameterized SQL queries."])
            validate_vectors(first, 384)
            np.testing.assert_allclose(first, second, rtol=1e-4, atol=1e-5)
            assert np.isclose(np.linalg.norm(first), 1, atol=1e-5)
            report["models"][model] = {
                "identity": provider.model_identity,
                "cold_load_ms": round(load_ms, 2),
                "rss_after_load_bytes": psutil.Process().memory_info().rss,
                "rss_before_model_switch_bytes": before_switch_rss,
                "files_bytes": sum(a["size"] for a in service.model_files.artifacts[model]),
                "index_id": index_id,
                "index": record["published"],
                "index_disk_bytes": record["disk_bytes"],
            }
            for query in queries:
                response = service.search(SearchRequest(query=query, index_id=index_id, model=model, top_k=5))
                if not response["results"]:
                    raise AppError(f"Real smoke: no results for {model}")
                report["queries"].append(
                    {
                        "model": model,
                        "index_id": index_id,
                        "query": query,
                        "request_id": response["request_id"],
                        "candidates": response["diagnostics"]["candidates_retrieved"],
                        "timings": response["diagnostics"]["timings"],
                        "results": compact_results(response["results"]),
                    }
                )
            for k in (3, 5, 10, 20):
                response = service.search(
                    SearchRequest(query=demo_queries[0], index_id=index_id, model=model, top_k=k)
                )
                assert len(response["results"]) == k
            empty = service.search(
                SearchRequest(
                    query=demo_queries[0], index_id=index_id, model=model, topic="__missing_topic__"
                )
            )
            assert not empty["results"]
            threshold = service.search(
                SearchRequest(query=demo_queries[0], index_id=index_id, model=model, similarity_threshold=1)
            )
            assert not threshold["results"]
            topic = service.search(
                SearchRequest(query=demo_queries[0], index_id=index_id, model=model, topic="Injection")
            )
            assert topic["results"] and all(r["topic"] == "Injection" for r in topic["results"])
            reranked = service.search(
                SearchRequest(query=demo_queries[0], index_id=index_id, model=model, reranking=True)
            )
            assert reranked["diagnostics"]["reranking_applied"] and reranked["comparison"]
            report["models"][model]["reranking_demo"] = {
                "rss_after_reranking_bytes": reranked["diagnostics"]["rss_after_bytes"],
                "query": demo_queries[0],
                "timings": reranked["diagnostics"]["timings"],
                "changed_positions": sum(r["delta"] != 0 for r in reranked["comparison"]),
                "before": compact_results(reranked["without_reranking"]),
                "after": compact_results(reranked["results"]),
                "comparison": reranked["comparison"],
            }
            service.models.unload("reranker")
            del provider
            report["checks"][model] = (
                "real inference, deterministic normalized 384D vectors, RU/EN search, Top-K, topic/threshold, actual reranking: passed"
            )
        service.models.unload()
        report["rss_after_unload_bytes"] = psutil.Process().memory_info().rss
        report["peak_sampled_rss_bytes"] = peak_rss[0]
        report["checks"]["result"] = "passed"
        output = settings.data_dir / "diagnostics" / "stage2-real-smoke.json"
        write_json(output, report)
        if args.export:
            from pathlib import Path

            write_json(Path(args.export), report)
        print(
            json.dumps(
                {
                    "result": "passed",
                    "queries": len(report["queries"]),
                    "offline": args.offline,
                    "report": str(output),
                    "models": report["checks"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except Exception as exc:
        report["checks"]["result"] = "failed"
        report["error"] = str(exc) if isinstance(exc, AppError) else type(exc).__name__
        write_json(settings.data_dir / "diagnostics/stage2-real-smoke-failed.json", report)
        print(json.dumps({"result": "failed", "error": report["error"]}, ensure_ascii=False))
        return 1
    finally:
        stop_sampling.set()
        sampler.join(timeout=1)


def compact_results(items):
    return [
        {
            "rank": r["final_rank"],
            "title": r["title"],
            "source_path": r["metadata"]["source_path"],
            "chunk_id": r["chunk_id"],
            "retrieval_rank": r["retrieval_rank"],
            "vector_similarity": r["vector_similarity"],
            "vector_distance": r["vector_distance"],
            "rerank_score": r["rerank_score"],
        }
        for r in items
    ]


if __name__ == "__main__":
    raise SystemExit(main())
