import json
import logging
from collections.abc import Callable

from src.core.models import Chunk, PreprocessingConfig
from src.storage.files import Store, atomic_write, digest, now, write_json

from .chunking import split
from .markdown import clean_markdown
from .tokenization import E5Tokenizer

log = logging.getLogger("owasp.preprocessing")


def preprocess(
    store: Store,
    config: PreprocessingConfig,
    tokenizer: E5Tokenizer,
    progress: Callable[..., None] = lambda **kw: None,
) -> dict:
    config_id = digest(json.dumps(config.model_dump(), sort_keys=True).encode())[:20]
    manifest = store.manifest()
    documents = sorted(
        (d for d in manifest.documents.values() if d.status == "active"), key=lambda d: d.document_id
    )
    lines, summary = [], {}
    for index, doc in enumerate(documents):
        cleaned, sections = clean_markdown(store.raw(doc.content_sha256).decode("utf-8"))
        count = 0
        for chunk_index, piece in enumerate(split(cleaned, sections, config, tokenizer)):
            chunk_hash = digest(piece.text.encode())
            chunk_id = digest(
                f"{doc.document_id}:{doc.content_sha256}:{config_id}:{chunk_index}:{chunk_hash}".encode()
            )
            chunk = Chunk(
                chunk_id=chunk_id,
                document_id=doc.document_id,
                title=doc.title,
                source_path=doc.source_path,
                url=doc.url,
                section=piece.heading_path[-1] if piece.heading_path else "",
                heading_path=piece.heading_path,
                chunk_index=chunk_index,
                chunking_strategy=config.strategy,
                chunk_size=config.chunk_size,
                overlap=config.overlap,
                content_sha256=chunk_hash,
                source_commit=doc.repository_commit,
                token_count=tokenizer.count(piece.text),
                text=piece.text,
                tokenizer=tokenizer.identity,
                configuration_id=config_id,
            )
            if not 0 < chunk.token_count <= config.chunk_size:
                raise ValueError("Chunk token budget invariant violated")
            lines.append(chunk.model_dump_json())
            count += 1
        summary[doc.document_id] = {"content_sha256": doc.content_sha256, "chunks": count}
        progress(completed=index + 1, total=len(documents), current=doc.title)
    output = ("\n".join(lines) + ("\n" if lines else "")).encode()
    filename = f"{config_id}-{digest(output)[:20]}.jsonl"
    atomic_write(store.root / "processed" / filename, output)
    run = {
        "configuration_id": config_id,
        "configuration": config.model_dump(),
        "file": filename,
        "created_at": now(),
        "source_commit": manifest.repository_commit,
        "documents": summary,
        "document_count": len(documents),
        "chunk_count": len(lines),
    }
    index = store.processed_index()
    index["runs"][config_id], index["latest"] = run, config_id
    write_json(store.root / "processed/index.json", index)
    log.info(
        "Preprocessing complete: %s documents, %s chunks",
        len(documents),
        len(lines),
        extra={"operation": "preprocess"},
    )
    return {key: run[key] for key in ("configuration_id", "document_count", "chunk_count", "file")}
