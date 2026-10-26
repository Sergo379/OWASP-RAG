"""Real Chroma persistence with atomic publication above the nontransactional SDK."""

import json
import re
import sqlite3
import time
from pathlib import Path

import numpy as np

from src.core.config import AppError
from src.core.models import Chunk
from src.embeddings.config import fingerprint
from src.embeddings.provider import validate_vectors
from src.storage.files import digest, now, read_json, write_json


def heading_only(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return bool(lines) and all(re.fullmatch(r"#{1,6}\s+\S.*", line) for line in lines)


def corpus_snapshot(manifest) -> dict:
    return {
        "commit": manifest.repository_commit,
        "documents": {
            key: {"sha256": doc.content_sha256, "topic": doc.topic}
            for key, doc in sorted(manifest.documents.items())
            if doc.status == "active"
        },
    }


def directory_bytes(root: Path) -> int:
    total = 0
    if root.exists():
        for path in root.rglob("*"):
            try:
                if path.is_file():
                    total += path.stat().st_size
            except FileNotFoundError:
                # SQLite/WAL or HNSW temporary files can be replaced during a read-only scan.
                continue
    return total


class EmbeddingCache:
    """Computation cache only; every searchable vector is also stored in Chroma."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS embeddings (key TEXT PRIMARY KEY, dim INTEGER, sha TEXT, vector BLOB)"
        )

    def get(self, key: str, dimension: int):
        row = self.db.execute("SELECT dim, sha, vector FROM embeddings WHERE key=?", (key,)).fetchone()
        if row is None or row[0] != dimension or digest(row[2]) != row[1]:
            return None
        vector = np.frombuffer(row[2], dtype="<f4").copy()
        if (
            vector.shape != (dimension,)
            or not np.isfinite(vector).all()
            or not np.isclose(np.linalg.norm(vector), 1, atol=1e-4)
        ):
            return None
        return vector

    def put(self, key, vector):
        data = np.asarray(vector, dtype="<f4").tobytes()
        self.db.execute(
            "INSERT OR REPLACE INTO embeddings VALUES (?,?,?,?)", (key, len(vector), digest(data), data)
        )

    def commit(self):
        self.db.commit()

    def close(self):
        self.db.close()


class VectorStore:
    def __init__(self, store, config, artifacts):
        self.store, self.config, self.artifacts = store, config, artifacts
        self.root = store.root / "vector_db"
        self.catalog_path = self.root / "catalog.json"
        self.clients = {}

    def catalog(self):
        catalog = read_json(self.catalog_path, {"schema_version": 1, "indexes": {}})
        if (
            not isinstance(catalog, dict)
            or catalog.get("schema_version") != 1
            or not isinstance(catalog.get("indexes"), dict)
        ):
            raise AppError(
                "Повреждён vector catalog. Сохраните копию vector_db; обратитесь к инструкции recovery"
            )
        return catalog

    def client(self, index_id: str):
        if not re.fullmatch(r"[0-9a-f]{32}", index_id):
            raise AppError("Некорректный index ID")
        if index_id not in self.clients:
            import chromadb
            from chromadb.config import Settings

            try:
                self.clients[index_id] = chromadb.PersistentClient(
                    path=str(self.root / index_id / "chroma"),
                    settings=Settings(anonymized_telemetry=False, allow_reset=False),
                )
            except Exception as exc:
                raise AppError(
                    "ChromaDB не открывается. Остановите приложение, сохраните копию vector_db и следуйте Recovery"
                ) from exc
        return self.clients[index_id]

    def collection(self, record):
        published = record.get("published")
        if not published:
            raise AppError("Индекс ещё не опубликован. Выполните Build Index или Resume")
        try:
            col = self.client(record["index_id"]).get_collection(
                published["collection_name"], embedding_function=None
            )
            if col.count() != published["vectors_count"]:
                raise ValueError("Count mismatch")
            return col
        except AppError:
            raise
        except Exception as exc:
            raise AppError(
                "Индекс повреждён или отсутствует. Выполните Rebuild Index; исходный корпус сохранён"
            ) from exc

    def identity(self, options):
        run = self.store.run(options.configuration_id)
        if not run:
            raise AppError("Нет выбранной конфигурации preprocessing. Сначала выполните preprocessing")
        manifest = self.store.manifest()
        snapshot = corpus_snapshot(manifest)
        if not snapshot["documents"]:
            raise AppError("Корпус пуст; сначала запустите Grabber и preprocessing")
        if set(run["documents"]) != set(snapshot["documents"]) or any(
            run["documents"][key]["content_sha256"] != value["sha256"]
            for key, value in snapshot["documents"].items()
        ):
            raise AppError(
                "Preprocessing не соответствует текущему корпусу. Повторите выбранную конфигурацию preprocessing"
            )
        spec = self.config.models[options.model]
        identity = {
            "schema_version": self.config.schema_version,
            "source_snapshot": snapshot,
            "source_snapshot_fingerprint": fingerprint(snapshot),
            "preprocessing_configuration_id": run["configuration_id"],
            "preprocessing_configuration": run["configuration"],
            "preprocessing_file": run["file"],
            "model": options.model,
            "model_identity": spec.identity,
            "model_spec": spec.model_dump(),
            "model_artifacts": self.artifacts[options.model],
            "embedding_configuration": {
                "pooling": "attention_mask_mean",
                "normalize": "l2",
                "dtype": "float32",
                "overflow_policy": options.overflow_policy,
                "heading_policy": options.heading_policy,
                "pipeline_version": "1",
                "distance": "cosine",
            },
        }
        index_id = fingerprint(identity)[:32]
        return index_id, identity, run

    def analysis(self, configuration_id=None):
        run = self.store.run(configuration_id)
        if not run:
            return {"total": 0, "heading_only": 0, "short": 0, "short_percent": 0, "distribution": {}}
        chunks = list(self.store.chunks(run))
        counts = [c["token_count"] for c in chunks]
        short = sum(c < self.config.short_chunk_tokens for c in counts)
        return {
            "configuration_id": run["configuration_id"],
            "total": len(chunks),
            "heading_only": sum(heading_only(c["text"]) for c in chunks),
            "short": short,
            "short_threshold": self.config.short_chunk_tokens,
            "short_percent": round(100 * short / len(chunks), 2) if chunks else 0,
            "distribution": {
                "min": min(counts, default=0),
                "median": float(np.median(counts)) if counts else 0,
                "p90": float(np.percentile(counts, 90)) if counts else 0,
                "max": max(counts, default=0),
            },
            "bins": {
                f"{a}-{b}": sum(a <= c <= b for c in counts)
                for a, b in [(1, 7), (8, 31), (32, 127), (128, 255), (256, 384)]
            },
        }

    def lists(self, operation=None):
        snapshot_hash = fingerprint(corpus_snapshot(self.store.manifest()))
        result = []
        for record in self.catalog()["indexes"].values():
            row = dict(record)
            attempt, published = row.get("attempt"), row.get("published")
            status = attempt["status"] if attempt else "Not Built"
            if status == "Building" and not (
                operation
                and operation["status"] == "running"
                and operation.get("index_id") == row["index_id"]
            ):
                status = "Interrupted"
            if status == "Ready" and row["identity"]["source_snapshot_fingerprint"] != snapshot_hash:
                status = "Stale"
            row.update(
                status=status,
                searchable=bool(published),
                disk_bytes=directory_bytes(self.root / row["index_id"]),
            )
            result.append(row)
        return sorted(result, key=lambda r: r["created_at"], reverse=True)

    def build(self, options, models, progress=lambda **kw: None):
        started = time.perf_counter()
        index_id, identity, run = self.identity(options)
        catalog = self.catalog()
        record = catalog["indexes"].get(index_id)
        if (
            record
            and record.get("published")
            and not options.rebuild
            and record.get("attempt", {}).get("status") == "Ready"
        ):
            self.collection(record)
            return {
                **record["published"],
                "index_id": index_id,
                "reused": True,
                "computed_vectors": 0,
                "cache_hits": 0,
                "reused_vectors": record["published"]["vectors_count"],
                "original_build_duration_ms": record["published"]["duration_ms"],
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            }
        provider = models.get(options.model)
        if provider.dimension != identity["model_spec"]["dimension"]:
            raise AppError("Размерность embedding-модели не совпала с конфигурацией")
        if record is None:
            record = {
                "index_id": index_id,
                "identity": identity,
                "created_at": now(),
                "published": None,
                "attempt": None,
                "updated_at": now(),
            }
            catalog["indexes"][index_id] = record
        previous = record.get("attempt")
        resume = bool(previous and previous["status"] != "Ready" and not options.rebuild)
        generation = previous["generation"] if resume else ((previous or {}).get("generation", 0) + 1)
        name = f"owasp_{index_id}_g{generation}"
        attempt = {
            "status": "Building",
            "generation": generation,
            "collection_name": name,
            "started_at": now(),
            "error": None,
        }
        record["attempt"] = attempt
        write_json(self.catalog_path, catalog)
        progress(
            index_id=index_id,
            current="Проверка snapshot и token budget",
            completed=0,
            total=run["chunk_count"],
        )
        cache = None
        try:
            chunks, rows, excluded = [], [], []
            seen = set()
            manifest = self.store.manifest()
            for value in self.store.chunks(run):
                chunk = Chunk.model_validate(value)
                if (
                    chunk.configuration_id != run["configuration_id"]
                    or chunk.chunk_id in seen
                    or digest(chunk.text.encode()) != chunk.content_sha256
                ):
                    raise AppError("Повреждён JSONL или повторяется chunk ID; повторите preprocessing")
                seen.add(chunk.chunk_id)
                chunks.append(chunk)
                if options.heading_policy == "skip_heading_only" and heading_only(chunk.text):
                    excluded.append({"chunk_id": chunk.chunk_id, "reason": "heading_only"})
                    continue
                for part_index, part in enumerate(
                    provider.document_parts(chunk.text, options.overflow_policy)
                ):
                    text_hash = digest(part["text"].encode())
                    vector_id = digest(f"{index_id}:{chunk.chunk_id}:{part_index}:{text_hash}".encode())
                    metadata = {
                        **chunk.model_dump(exclude={"text", "heading_path"}),
                        "heading_path": json.dumps(chunk.heading_path, ensure_ascii=False),
                        "topic": manifest.documents[chunk.document_id].topic,
                        "source_content_sha256": run["documents"][chunk.document_id]["content_sha256"],
                        "segment_index": part_index,
                        "char_start": part["char_start"],
                        "char_end": part["char_end"],
                        "model_input_tokens": part["model_input_tokens"],
                        "indexed_content_sha256": text_hash,
                        "embedding_transformation": "identity"
                        if len(part["text"]) == len(chunk.text)
                        else "token_budget_split",
                        "index_id": index_id,
                        "model_identity": provider.model_identity,
                    }
                    rows.append({"id": vector_id, "text": part["text"], "metadata": metadata})
            if len(chunks) != run["chunk_count"]:
                raise AppError("Число chunks не совпадает с run summary")
            if not rows:
                raise AppError("Нет фрагментов для индексирования после применения политики")
            client = self.client(index_id)
            collection = client.get_or_create_collection(
                name,
                embedding_function=None,
                configuration={"hnsw": {"space": "cosine", "num_threads": self.config.cpu_threads}},
                metadata={
                    "index_id": index_id,
                    "model_identity": provider.model_identity,
                    "schema_version": 1,
                },
            )
            cache = EmbeddingCache(self.root / "embedding_cache.sqlite")
            completed, computed, hits = 0, 0, 0
            processed_chunks = set()
            for batch_index, start in enumerate(range(0, len(rows), self.config.batch_size)):
                batch = rows[start : start + self.config.batch_size]
                present = set(collection.get(ids=[r["id"] for r in batch], include=[])["ids"])
                pending = [r for r in batch if r["id"] not in present]
                vectors, missing, keys = {}, [], {}
                for row in pending:
                    key = fingerprint(
                        {
                            "model": identity["model_spec"],
                            "artifacts": identity["model_artifacts"],
                            "text": row["text"],
                            "pooling": "mean-l2-float32-v1",
                        }
                    )
                    keys[row["id"]] = key
                    vector = cache.get(key, provider.dimension)
                    if vector is None:
                        missing.append(row)
                    else:
                        vectors[row["id"]] = vector
                        hits += 1
                if missing:
                    outputs = validate_vectors(
                        provider.embed_documents([r["text"] for r in missing]), provider.dimension
                    )
                    if len(outputs) != len(missing):
                        raise AppError("Embedding provider вернул неверное число векторов")
                    for row, vector in zip(missing, outputs, strict=True):
                        vectors[row["id"]] = vector
                        cache.put(keys[row["id"]], vector)
                    cache.commit()
                    computed += len(missing)
                if pending:
                    collection.upsert(
                        ids=[r["id"] for r in pending],
                        documents=[r["text"] for r in pending],
                        metadatas=[r["metadata"] for r in pending],
                        embeddings=[vectors[r["id"]].tolist() for r in pending],
                    )
                completed += len(batch)
                processed_chunks.update(r["metadata"]["chunk_id"] for r in batch)
                progress(
                    index_id=index_id,
                    completed=completed,
                    total=len(rows),
                    batch=batch_index + 1,
                    batches=(len(rows) + self.config.batch_size - 1) // self.config.batch_size,
                    current=f"Индексирование {options.model}",
                    units="vectors",
                    elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
                    source_chunks=len(chunks),
                    processed_source_chunks=len(processed_chunks),
                    excluded_chunks=len(excluded),
                )
            actual_ids = set(collection.get(include=[])["ids"])
            if actual_ids != {r["id"] for r in rows} or collection.count() != len(rows):
                raise AppError("Проверка готовой Chroma collection не прошла; индекс не опубликован")
            # Persist enough original metadata to inspect any old generation without modifying stage 1 JSONL.
            sidecar = {
                "original_chunks": {c.chunk_id: c.model_dump() for c in chunks},
                "excluded": excluded,
                "analysis": self.analysis(run["configuration_id"]),
            }
            write_json(self.root / index_id / f"g{generation}.json", sidecar)
            published = {
                "collection_name": name,
                "generation": generation,
                "vectors_count": len(rows),
                "source_chunks": len(chunks),
                "indexed_chunks": len({r["metadata"]["chunk_id"] for r in rows}),
                "indexed_documents": len({r["metadata"]["document_id"] for r in rows}),
                "excluded_count": len(excluded),
                "split_vectors": sum(r["metadata"]["embedding_transformation"] != "identity" for r in rows),
                "dimension": provider.dimension,
                "created_at": now(),
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                "computed_vectors": computed,
                "cache_hits": hits,
            }
            record.update(published=published, updated_at=now())
            attempt.update(status="Ready", finished_at=now())
            write_json(self.catalog_path, catalog)  # Only this atomic pointer publishes a generation.
            return {"index_id": index_id, "reused": False, **published}
        except BaseException as exc:
            attempt.update(
                status="Interrupted" if not isinstance(exc, Exception) else "Failed",
                error=str(exc) if isinstance(exc, AppError) else "Ошибка построения; см. технические логи",
                finished_at=now(),
            )
            write_json(self.catalog_path, catalog)  # Previous published generation is retained.
            raise
        finally:
            if cache:
                cache.close()

    def inspect(self, index_id: str, operation=None):
        record = next((r for r in self.lists(operation) if r["index_id"] == index_id), None)
        if not record:
            raise AppError("Индекс не найден")
        if record.get("published"):
            col = self.collection(record)
            record["actual_vectors_count"] = col.count()
            record["inspection"] = read_json(
                self.root / index_id / f"g{record['published']['generation']}.json"
            )
            # Return summaries, not thousands of full original chunks, from the management API.
            record["inspection"].pop("original_chunks", None)
        return record
