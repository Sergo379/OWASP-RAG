"""Explicit Chat-only lexical/semantic fusion; baseline Search remains unchanged."""

import json
import re
import time
from collections import Counter

import numpy as np

from src.core.config import AppError
from src.retrieval.lexical import BM25, named_controls, terms
from src.retrieval.pipeline import observed
from src.storage.files import read_json


def summary(row):
    return {
        key: row.get(key)
        for key in (
            "vector_id",
            "chunk_id",
            "document_id",
            "title",
            "section",
            "url",
            "retrieval_rank",
            "vector_similarity",
            "rerank_score",
            "lexical_rank",
            "lexical_score",
            "fusion_score",
            "final_rank",
            "fusion_rank",
            "rerank_rank",
        )
    }


def eligible(row, request, manifest):
    meta = row["metadata"]
    doc = manifest.documents.get(row["document_id"])
    return (
        doc is not None
        and doc.status == "active"
        and doc.content_sha256 == meta["source_content_sha256"]
        and (not request.topic or doc.topic == request.topic)
        and (not request.document_id or row["document_id"] == request.document_id)
    )


def index_rows(vectors, record):
    key = (record["index_id"], record["published"]["generation"])
    cache = getattr(vectors, "_chat_lexical", {})
    if key not in cache:
        original = read_json(vectors.root / key[0] / f"g{key[1]}.json")["original_chunks"]
        collection = vectors.collection(record)
        data = collection.get(include=["metadatas", "documents"])
        unique = {}
        for vector_id, meta, text in zip(data["ids"], data["metadatas"], data["documents"], strict=True):
            if meta["index_id"] != key[0] or meta["model_identity"] != record["identity"]["model_identity"]:
                raise AppError("Metadata lexical candidate не соответствует выбранному индексу")
            chunk_id = meta["chunk_id"]
            row = {
                "vector_id": vector_id,
                "chunk_id": chunk_id,
                "document_id": meta["document_id"],
                "title": meta["title"],
                "topic": meta["topic"],
                "url": meta["url"],
                "section": meta["section"],
                "heading_path": json.loads(meta["heading_path"]),
                "text": text,
                "original_text": original[chunk_id]["text"],
                "metadata": meta,
                "retrieval_rank": None,
                "rerank_score": None,
            }
            if (
                chunk_id not in unique
                or meta["segment_index"] < unique[chunk_id]["metadata"]["segment_index"]
            ):
                unique[chunk_id] = row
        rows = sorted(unique.values(), key=lambda r: r["chunk_id"])
        cache[key] = BM25(rows)
        # One published lexical snapshot at a time, without deleting any on-disk data.
        vectors._chat_lexical = {key: cache[key]}
    return cache[key]


def retrieve_for_chat(request, vectors, models, config, *, request_id, observer, search, document_focus=True):
    started = time.perf_counter()
    base_request = request.model_copy(update={"reranking": False})
    base = search(
        base_request, vectors, models, config, request_id=request_id, observer=observer, include_pool=True
    )
    record = vectors.catalog()["indexes"][request.index_id]
    manifest = vectors.store.manifest()
    semantic = base["all_candidates"]
    stage_started = time.perf_counter()
    with observed(observer, "LEXICAL_RETRIEVAL", as_type="retriever") as span:
        lexical = index_rows(vectors, record)
        query_counts = Counter(terms(request.query))
        generic = {"security", "data", "verify", "config", "owasp", "cheat", "sheet", "what", "how", "which"}
        doc_scores = {
            r["document_id"]: sum(query_counts[t] for t in set(terms(r["title"])) - generic)
            for r in lexical.rows
        }
        maximum = max(doc_scores.values(), default=0)
        focus = {key for key, score in doc_scores.items() if maximum and score == maximum}
        all_ranked = [
            (score, row)
            for score, row in lexical.rank(request.query, len(lexical.rows))
            if eligible(row, request, manifest)
        ]
        focused = [pair for pair in all_ranked if pair[1]["document_id"] in focus][
            : request.candidate_pool_size // 2
        ]
        ids = {row["chunk_id"] for _, row in focused}
        ranked = (focused + [pair for pair in all_ranked if pair[1]["chunk_id"] not in ids])[
            : request.candidate_pool_size
        ]
        vectors_by_id = {}
        if ranked:
            data = vectors.collection(record).get(
                ids=[row["vector_id"] for _, row in ranked], include=["embeddings"]
            )
            vectors_by_id = dict(zip(data["ids"], data["embeddings"], strict=True))
        query = np.asarray(base["_query_embedding"], dtype=np.float32)
        lexical_rows = []
        for rank, (score, row) in enumerate(ranked, 1):
            vector = np.asarray(vectors_by_id[row["vector_id"]], dtype=np.float32)
            if vector.shape != query.shape or not np.isfinite(vector).all() or np.linalg.norm(vector) == 0:
                raise AppError("Некорректный lexical candidate embedding")
            cosine = float(np.dot(query, vector) / (np.linalg.norm(query) * np.linalg.norm(vector)))
            if cosine >= request.similarity_threshold:
                lexical_rows.append(
                    {
                        **row,
                        "lexical_rank": rank,
                        "lexical_score": score,
                        "vector_similarity": cosine,
                        "vector_distance": 1 - cosine,
                    }
                )
        span.update(
            metadata={
                "index_id": request.index_id,
                "generation": record["published"]["generation"],
                "eligible": len(lexical_rows),
                "chunk_ids": [r["chunk_id"] for r in lexical_rows],
            }
        )
    lexical_ms = (time.perf_counter() - stage_started) * 1000
    stage_started = time.perf_counter()
    with observed(observer, "HYBRID_FUSION") as span:
        merged = {}
        for row in semantic:
            merged[row["chunk_id"]] = {
                **row,
                "fusion_score": 1 / (60 + row["retrieval_rank"]),
                "lexical_rank": None,
                "lexical_score": None,
            }
        for row in lexical_rows:
            key = row["chunk_id"]
            if key in merged:
                merged[key].update(lexical_rank=row["lexical_rank"], lexical_score=row["lexical_score"])
                merged[key]["fusion_score"] += 1 / (60 + row["lexical_rank"])
            else:
                merged[key] = {**row, "fusion_score": 1 / (60 + row["lexical_rank"])}
        ranked_rows = sorted(merged.values(), key=lambda r: (-r["fusion_score"], r["chunk_id"]))[
            : request.candidate_pool_size
        ]
        for rank, row in enumerate(ranked_rows, 1):
            row.update(fusion_rank=rank, final_rank=None, rerank_rank=None)
        span.update(
            metadata={
                "semantic": len(semantic),
                "lexical": len(lexical_rows),
                "merged": len(ranked_rows),
                "method": "RRF k=60; cosine threshold remains active",
            }
        )
    comparison = []
    fusion_ms = (time.perf_counter() - stage_started) * 1000
    stage_started = time.perf_counter()
    with observed(observer, "CHAT_RERANKING") as span:
        if request.reranking and ranked_rows:
            provider = models.get("reranker")
            scores, _ = provider.rerank(
                request.query,
                [r["original_text"] for r in ranked_rows],
                config.batch_size,
                config.reranker_passage_tokens,
            )
            for row, score in zip(ranked_rows, scores, strict=True):
                row["rerank_score"] = score
            ranked_rows.sort(key=lambda r: (-r["rerank_score"], -r["fusion_score"], r["chunk_id"]))
            for rank, row in enumerate(ranked_rows, 1):
                row["rerank_rank"] = rank
            comparison = [summary(r) for r in ranked_rows]
        span.update(
            metadata={
                "status": "completed" if request.reranking else "skipped",
                "candidates": len(ranked_rows),
                "chunk_ids": [r["chunk_id"] for r in ranked_rows],
                "scores": [r["rerank_score"] for r in ranked_rows] if request.reranking else [],
            }
        )
    rerank_ms = (time.perf_counter() - stage_started) * 1000
    stage_started = time.perf_counter()
    # Greedy coverage of requested terms, with a soft repetition penalty for the same section.
    # This is context selection within the fused pool, not a change to semantic Search.
    with observed(observer, "CONTEXT_CANDIDATE_SELECTION") as span:
        selected = []
        covered = set()
        sections = set()
        remaining = list(ranked_rows)
        query_terms = set(terms(request.query)) - generic
        selection_removed = [
            {"chunk_id": r["chunk_id"], "reason": "reference_list_or_heading_only"}
            for r in remaining
            if r["section"].casefold() in ("references", "external links", "table of contents")
            or not re.sub(r"(?m)^\s*#+.*$", "", r["original_text"]).strip()
        ]
        removed_ids = {r["chunk_id"] for r in selection_removed}
        remaining = [r for r in remaining if r["chunk_id"] not in removed_ids]
        identifiers = named_controls(request.query)
        scoped_scores = {r["document_id"]: doc_scores.get(r["document_id"], 0) for r in remaining}
        scoped_maximum = max(scoped_scores.values(), default=0)
        focused_context = set()
        if document_focus and scoped_maximum >= 2 and not identifiers:
            focused_context = {doc for doc, score in scoped_scores.items() if score >= scoped_maximum / 2}
            outside = [r for r in remaining if r["document_id"] not in focused_context]
            selection_removed.extend(
                {"chunk_id": r["chunk_id"], "reason": "strong_inferred_document_scope"} for r in outside
            )
            remaining = [r for r in remaining if r["document_id"] in focused_context]
        if len(identifiers) == 1:
            identifier = next(iter(identifiers))
            subjects = set(terms(request.query)) & {"cookie", "password", "encoding", "hash"}
            unrelated = [
                r
                for r in remaining
                if not re.search(r"\b" + re.escape(identifier) + r"\b", r["original_text"], re.I)
                or (subjects and not subjects.intersection(terms(r["original_text"])))
            ]
            selection_removed.extend(
                {"chunk_id": r["chunk_id"], "reason": "named_control_absent"} for r in unrelated
            )
            remaining = [r for r in remaining if r not in unrelated]
            exact_headings = [
                r for r in remaining if re.search(r"\b" + re.escape(identifier) + r"\b", r["section"], re.I)
            ]
            if exact_headings:
                selection_removed.extend(
                    {"chunk_id": r["chunk_id"], "reason": "exact_control_section_preferred"}
                    for r in remaining
                    if r not in exact_headings
                )
                remaining = exact_headings
        while remaining and len(selected) < request.top_k:

            def priority(row):
                title_terms = set(terms(row["title"] + " " + row["section"]))
                novelty = len((title_terms & query_terms) - covered)
                section_match = len(
                    (set(terms(row["section"])) & query_terms) - {"transport", "confidential", "api", "rest"}
                )
                named_heading = any(
                    re.search(r"\b" + re.escape(name) + r"\b", row["section"], re.I) for name in identifiers
                )
                same = (row["document_id"], row["section"]) in sections
                # A general certificate question should reach server trust/CA sections
                # before the narrower mutual/client-certificate alternative.
                unrequested_scope = any(
                    re.search(section_pattern, row["section"], re.I)
                    and not re.search(query_pattern, request.query, re.I)
                    for section_pattern, query_pattern in (
                        (r"client certificates|mutual TLS", r"client|mutual|клиент|взаимн"),
                        (r"wildcard", r"wildcard|подстанов|маск|\*\."),
                    )
                )
                # Reranker remains authoritative when requested; novelty is only a tie-break then.
                base_score = row["rerank_score"] if request.reranking else row["fusion_score"]
                return (
                    base_score
                    if request.reranking
                    else base_score
                    + 0.008 * novelty
                    + 0.02 * section_match
                    + (0.02 * doc_scores.get(row["document_id"], 0) / maximum if maximum else 0)
                    + (0.04 if named_heading else 0)
                    - (0.02 if unrequested_scope else 0)
                    - 0.02 * same,
                    novelty,
                    row["chunk_id"],
                )

            row = max(remaining, key=priority)
            remaining.remove(row)
            if any(r["original_text"] == row["original_text"] for r in selected):
                continue
            selected.append(row)
            covered.update(set(terms(row["title"] + " " + row["section"])) & query_terms)
            sections.add((row["document_id"], row["section"]))
        for rank, row in enumerate(selected, 1):
            row["final_rank"] = rank
        span.update(
            metadata={
                "selected_chunk_ids": [r["chunk_id"] for r in selected],
                "requested_top_k": request.top_k,
            }
        )
    diagnostics = {
        **base["diagnostics"],
        "parameters": request.model_dump(exclude={"query"}),
        "retrieval_strategy": "hybrid_context",
        "baseline_timings": base["diagnostics"].get("timings", {}),
        "baseline_final_result_count": base["diagnostics"].get("final_result_count"),
        "final_result_count": len(selected),
        "timings": {
            **base["diagnostics"].get("timings", {}),
            "lexical_ms": round(lexical_ms, 3),
            "fusion_ms": round(fusion_ms, 3),
            "reranking_ms": round(rerank_ms, 3) if request.reranking else 0.0,
            "context_selection_ms": round((time.perf_counter() - stage_started) * 1000, 3),
            "total_ms": round((time.perf_counter() - started) * 1000, 3),
        },
        "selection_removed": selection_removed,
        "focused_document_ids": sorted(focus),
        "context_document_focus": {
            "enabled": document_focus,
            "strong_match_score": scoped_maximum,
            "selected_document_ids": sorted(focused_context),
            "policy": "Eligible title overlap >=2; retain titles >=half of strongest score; named-control questions exempt",
        },
        "document_title_scores": doc_scores,
        "candidate_pool": [summary(r) for r in ranked_rows],
        "semantic_candidate_pool": base["diagnostics"].get("candidate_pool", []),
        "selected_candidates": [summary(r) for r in selected],
        "reranking_enabled": request.reranking,
        "reranking_applied": bool(request.reranking and ranked_rows),
        "chat_total_ms": round((time.perf_counter() - started) * 1000, 3),
        "fusion": "RRF 60 + metadata-title affinity, requested-section match and term coverage; no probability interpretation",
        "scope_policy": "Unrequested mutual/client/wildcard certificate sections receive a soft penalty; optional reranker scores remain authoritative",
    }
    return {
        "request_id": request_id,
        "results": selected,
        "baseline_semantic_top_k": base["results"],
        "comparison": comparison,
        "diagnostics": diagnostics,
    }
