"""One candidate pool for plain/reranked views, with explicit score semantics."""

import json
import logging
import time
import uuid
from contextlib import contextmanager

import psutil

from src.core.config import AppError
from src.embeddings.config import fingerprint
from src.embeddings.provider import validate_vectors
from src.storage.files import read_json
from src.storage.vectors import corpus_snapshot

log = logging.getLogger("owasp.search")


@contextmanager
def observed(observer, name, **fields):
    if observer is None:
        from src.observability.tracing import NullSpan
        yield NullSpan()
    else:
        with observer.step(name, **fields) as span:
            yield span


def filter_candidates(candidates: list[dict], request, manifest, observer=None):
    kept, removed, reasons = [], [], {}
    # Separate real passes; stale identity retains priority in the public diagnostics.
    with observed(observer, "SIMILARITY_FILTERING") as span:
        for candidate in candidates:
            if candidate["vector_similarity"] < request.similarity_threshold:
                reasons[candidate["vector_id"]] = "below_threshold"
        span.update(metadata={"threshold": request.similarity_threshold, "below_threshold": len(reasons)})
    with observed(observer, "METADATA_FILTERING") as span:
        for candidate in candidates:
            meta = candidate["metadata"]
            doc = manifest.documents.get(meta["document_id"])
            if not doc or doc.status != "active" or doc.content_sha256 != meta["source_content_sha256"]:
                reasons[candidate["vector_id"]] = "stale"
            elif candidate["vector_id"] not in reasons and ((request.topic and meta["topic"] != request.topic) or (
                request.document_id and meta["document_id"] != request.document_id
            )):
                reasons[candidate["vector_id"]] = "metadata_mismatch"
        span.update(metadata={"removed": len(reasons), "topic": request.topic, "document_id": request.document_id})
    for candidate in candidates:
        meta = candidate["metadata"]
        reason = reasons.get(candidate["vector_id"])
        if reason:
            removed.append(
                {"vector_id": candidate["vector_id"], "chunk_id": meta["chunk_id"], "reason": reason}
            )
        else:
            kept.append(candidate)
    return kept, removed


def deduplicate(candidates: list[dict]):
    kept, removed, chunks, texts = [], [], set(), set()
    for candidate in candidates:
        meta = candidate["metadata"]
        if meta["chunk_id"] in chunks or meta["content_sha256"] in texts:
            removed.append(
                {"vector_id": candidate["vector_id"], "chunk_id": meta["chunk_id"], "reason": "duplicate"}
            )
        else:
            chunks.add(meta["chunk_id"])
            texts.add(meta["content_sha256"])
            kept.append(candidate)
    return kept, removed


def execute_search(request, vectors, models, config, *, request_id=None, observer=None) -> dict:
    request_id, started = request_id or uuid.uuid4().hex, time.perf_counter()
    timings, stages = {}, []
    rss_before = psutil.Process().memory_info().rss

    def event(name, **context):
        stages.append(
            {"event": name, "elapsed_ms": round((time.perf_counter() - started) * 1000, 3), **context}
        )
        log.info(name, extra={"operation": "search", "context": {"request_id": request_id, **context}})

    def elapsed(since):
        return round((time.perf_counter() - since) * 1000, 3)

    event(
        "QUERY_RECEIVED",
        **({"query": request.query} if config.log_queries else {"query_chars": len(request.query)}),
    )
    try:
        step = time.perf_counter()
        if (
            len(request.query) > config.query_max_chars
            or request.candidate_pool_size > config.max_candidate_pool
        ):
            raise AppError("Запрос или candidate pool превышает настройки retrieval.yaml")
        record = vectors.catalog()["indexes"].get(request.index_id)
        if not record:
            raise AppError("Индекс не найден. Сначала выполните Build Index")
        identity = record["identity"]
        if (
            request.model != identity["model"]
            or config.models[request.model].identity != identity["model_identity"]
        ):
            raise AppError("Выбранная модель не соответствует индексу. Выберите индекс этой модели")
        if identity["model_artifacts"] != vectors.artifacts[request.model]:
            raise AppError("Weights identity изменён; постройте отдельный индекс")
        timings["validation_ms"] = elapsed(step)
        event("QUERY_VALIDATED", model=request.model, index_id=request.index_id, top_k=request.top_k)
        step = time.perf_counter()
        provider = models.get(request.model)
        if not request.reranking:
            models.unload("reranker")
        timings["model_ready_ms"] = elapsed(step)
        event("MODEL_READY", model_identity=provider.model_identity, duration_ms=timings["model_ready_ms"])
        step = time.perf_counter()
        with observed(observer, "QUERY_EMBEDDING", as_type="embedding",
                      model=provider.model_identity) as span:
            embedding = validate_vectors([provider.embed_query(request.query)], provider.dimension)[0]
            span.update(metadata={"dimension": len(embedding), "query_chars": len(request.query)})
        if len(embedding) != identity["model_spec"]["dimension"]:
            raise AppError("Размерность query embedding не соответствует индексу")
        timings["query_embedding_ms"] = elapsed(step)
        event("QUERY_EMBEDDED", dimension=len(embedding), duration_ms=timings["query_embedding_ms"])
        step = time.perf_counter()
        collection = vectors.collection(record)
        where_parts = []
        if request.topic:
            where_parts.append({"topic": request.topic})
        if request.document_id:
            where_parts.append({"document_id": request.document_id})
        where = {"$and": where_parts} if len(where_parts) > 1 else (where_parts[0] if where_parts else None)
        event("VECTOR_RETRIEVAL_STARTED", pool_size=request.candidate_pool_size, metadata_pushdown=where)
        with observed(observer, "VECTOR_RETRIEVAL", as_type="retriever") as span:
            response = collection.query(
                query_embeddings=[embedding.tolist()],
                n_results=min(request.candidate_pool_size, collection.count()),
                where=where,
                include=["documents", "metadatas", "distances"],
            )
            span.update(metadata={"index_id": request.index_id, "snapshot": identity["source_snapshot_fingerprint"],
                                  "candidate_ids": response["ids"][0], "distances": response["distances"][0],
                                  "metadata_pushdown": where})
        candidates = [
            {
                "vector_id": vector_id,
                "text": text,
                "metadata": meta,
                "vector_distance": float(distance),
                "vector_similarity": float(1 - distance),
            }
            for vector_id, text, meta, distance in zip(
                response["ids"][0],
                response["documents"][0],
                response["metadatas"][0],
                response["distances"][0],
                strict=True,
            )
        ]
        candidates.sort(key=lambda c: (c["vector_distance"], c["vector_id"]))
        for rank, candidate in enumerate(candidates, 1):
            candidate["retrieval_rank"] = rank
            if (
                candidate["metadata"]["model_identity"] != provider.model_identity
                or candidate["metadata"]["index_id"] != request.index_id
            ):
                raise AppError("Metadata вектора не соответствует model/index identity; выполните Rebuild")
        timings["retrieval_ms"] = elapsed(step)
        event(
            "VECTOR_RETRIEVAL_COMPLETED",
            candidates=len(candidates),
            duration_ms=timings["retrieval_ms"],
            candidate_ids=[c["metadata"]["chunk_id"] for c in candidates],
            scores=[c["vector_similarity"] for c in candidates],
        )
        step = time.perf_counter()
        manifest = vectors.store.manifest()
        kept, filtered = filter_candidates(candidates, request, manifest, observer)
        timings["filtering_ms"] = elapsed(step)
        event("FILTERING_COMPLETED", kept=len(kept), removed=filtered, duration_ms=timings["filtering_ms"])
        step = time.perf_counter()
        with observed(observer, "DEDUPLICATION") as span:
            kept, duplicates = deduplicate(kept)
            span.update(metadata={"kept": len(kept), "removed": duplicates})
        timings["deduplication_ms"] = elapsed(step)
        event(
            "DEDUPLICATION_COMPLETED",
            kept=len(kept),
            removed=duplicates,
            duration_ms=timings["deduplication_ms"],
        )
        generation = record["published"]["generation"]
        originals = read_json(vectors.root / request.index_id / f"g{generation}.json")["original_chunks"]
        for candidate in kept:
            candidate["original_text"] = originals[candidate["metadata"]["chunk_id"]]["text"]
        baseline = list(kept)
        comparison = []
        timings["reranking_ms"] = 0.0
        if request.reranking and kept:
            step = time.perf_counter()
            event("RERANKING_STARTED", candidates=len(kept), model=config.models["reranker"].identity)
            with observed(observer, "RERANKING", model=config.models["reranker"].identity) as span:
                reranker = models.get("reranker")
                scores, transformations = reranker.rerank(
                    request.query,
                    [c["original_text"] for c in kept],
                    config.batch_size,
                    config.reranker_passage_tokens,
                )
                span.update(metadata={"candidate_ids": [c["metadata"]["chunk_id"] for c in kept],
                                      "scores": scores, "status": "completed"})
            for candidate, score, transformation in zip(kept, scores, transformations, strict=True):
                candidate["rerank_score"] = score
                candidate["rerank_transformation"] = transformation
            kept = sorted(kept, key=lambda c: (-c["rerank_score"], c["retrieval_rank"], c["vector_id"]))
            for new_rank, candidate in enumerate(kept, 1):
                comparison.append(
                    {
                        "chunk_id": candidate["metadata"]["chunk_id"],
                        "original_rank": candidate["retrieval_rank"],
                        "new_rank": new_rank,
                        "vector_similarity": candidate["vector_similarity"],
                        "vector_distance": candidate["vector_distance"],
                        "rerank_score": candidate["rerank_score"],
                        "delta": candidate["retrieval_rank"] - new_rank,
                    }
                )
            timings["reranking_ms"] = elapsed(step)
            event("RERANKING_COMPLETED", duration_ms=timings["reranking_ms"], ranking=comparison)
        else:
            with observed(observer, "RERANKING") as span:
                span.update(metadata={"status": "skipped", "enabled": request.reranking, "candidates": len(kept)})
            event("RERANKING_SKIPPED", enabled=request.reranking, candidates=len(kept))

        def present(items, reranked):
            output = []
            for rank, candidate in enumerate(items[: request.top_k], 1):
                meta = candidate["metadata"]
                output.append(
                    {
                        "vector_id": candidate["vector_id"],
                        "chunk_id": meta["chunk_id"],
                        "document_id": meta["document_id"],
                        "title": meta["title"],
                        "topic": meta["topic"],
                        "url": meta["url"],
                        "section": meta["section"],
                        "heading_path": json.loads(meta["heading_path"]),
                        "text": candidate["text"],
                        "original_text": candidate["original_text"],
                        "retrieval_rank": candidate["retrieval_rank"],
                        "final_rank": rank,
                        "vector_distance": candidate["vector_distance"],
                        "vector_similarity": candidate["vector_similarity"],
                        "rerank_score": candidate.get("rerank_score") if reranked else None,
                        "metadata": {
                            **meta,
                            **(
                                {"rerank_transformation": candidate["rerank_transformation"]}
                                if reranked and "rerank_transformation" in candidate
                                else {}
                            ),
                        },
                    }
                )
            return output

        timings["total_ms"] = elapsed(started)
        stale_index = identity["source_snapshot_fingerprint"] != fingerprint(corpus_snapshot(manifest))
        diagnostics = {
            "request_id": request_id,
            "query": request.query,
            "parameters": request.model_dump(exclude={"query"}),
            "model_identity": provider.model_identity,
            "index_id": request.index_id,
            "source_snapshot": identity["source_snapshot"],
            "source_snapshot_fingerprint": identity["source_snapshot_fingerprint"],
            "preprocessing_configuration_id": identity["preprocessing_configuration_id"],
            "preprocessing_configuration": identity["preprocessing_configuration"],
            "vector_dimension": provider.dimension,
            "distance_metric": "cosine",
            "similarity_formula": "1 - cosine_distance",
            "candidate_pool_size": request.candidate_pool_size,
            "candidates_retrieved": len(candidates),
            "filtered_count": len(filtered),
            "deduplicated_count": len(duplicates),
            "removed": filtered + duplicates,
            "reranking_enabled": request.reranking,
            "reranking_applied": bool(request.reranking and kept),
            "reranker_score_semantics": "raw logit; max over explicit passage windows; not a probability",
            "final_result_count": min(len(kept), request.top_k),
            "eligible_before_top_k": len(kept),
            "stale_index": stale_index,
            "warnings": [
                "Индекс относится к предыдущему snapshot; неактуальные chunks отфильтрованы. Постройте новый индекс."
            ]
            if stale_index
            else [],
            "timings": timings,
            "rss_before_bytes": rss_before,
            "rss_after_bytes": psutil.Process().memory_info().rss,
            "stages": stages,
        }
        event("RESULTS_READY", returned=diagnostics["final_result_count"], total_ms=timings["total_ms"])
        return {
            "request_id": request_id,
            "results": present(kept, request.reranking),
            "without_reranking": present(baseline, False),
            "comparison": comparison,
            "diagnostics": diagnostics,
        }
    except Exception as exc:
        event("SEARCH_FAILED", error_type=type(exc).__name__, total_ms=elapsed(started))
        if isinstance(exc, AppError):
            raise
        raise AppError(
            "Ошибка retrieval. Проверьте опубликованный индекс и logs; при повреждении выполните Rebuild"
        ) from exc
