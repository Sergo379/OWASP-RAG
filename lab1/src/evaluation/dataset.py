from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from src.core.config import AppError
from src.storage.files import Store, digest, read_json
from src.storage.vectors import corpus_snapshot

CATEGORIES = {"factual", "technical", "multi_document", "engineering", "unanswerable"}
SPLITS = {"development": 10, "final": 30}


@dataclass(frozen=True)
class Dataset:
    questions: tuple[dict, ...]
    manifest: dict

    def split(self, name: str):
        if name not in SPLITS:
            raise AppError("Неизвестный split")
        return [q for q in self.questions if q["split"] == name]


def load_dataset(root: Path, store: Store | None = None) -> Dataset:
    directory = root / "experiments/datasets"
    path = directory / "owasp-v1.json"
    manifest = read_json(directory / "owasp-v1.manifest.json")
    if not manifest or manifest.get("schema_version") != 1 or not path.is_file():
        raise AppError("Dataset или manifest отсутствует")
    if digest(path.read_bytes()) != manifest.get("sha256"):
        raise AppError("Контрольная сумма dataset не совпала")
    questions = read_json(path)
    try:
        verify(questions, manifest, store)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise AppError(f"Некорректный dataset: {exc}") from exc
    return Dataset(tuple(questions), manifest)


def verify(questions: list, manifest: dict, store: Store | None) -> None:
    snapshot = manifest["corpus_snapshot"]
    if len(questions) != 40 or manifest["expected_count"] != 40:
        raise ValueError("ожидается 40 вопросов")
    for key in ("id", "question"):
        if len({q[key].strip().casefold() for q in questions}) != len(questions):
            raise ValueError(f"повторяющиеся значения {key}")
    distribution = Counter((q["split"], q["category"], q["language"]) for q in questions)
    if distribution != {
        (split, category, language): size // 10
        for split, size in SPLITS.items()
        for category in CATEGORIES
        for language in ("ru", "en")
    }:
        raise ValueError("нарушен баланс языка, категории или split")
    local_docs = {}
    if store is not None:
        local_manifest = store.manifest()
        if corpus_snapshot(local_manifest) != snapshot:
            raise ValueError("локальный корпус отличается от зафиксированного snapshot")
        local_docs = {
            key: store.raw(info["sha256"]).decode("utf-8") for key, info in snapshot["documents"].items()
        }
    for q in questions:
        ident = q["id"]
        for field in ("id", "question", "reference_answer", "topic", "annotation_origin"):
            if not q[field].strip():
                raise ValueError(f"{ident}: пустое поле {field}")
        if q["source_snapshot"] != snapshot["commit"] or q["difficulty"] not in ("easy", "medium", "hard"):
            raise ValueError(f"{ident}: snapshot или difficulty")
        claims, evidence = q["expected_claims"], q["source_evidence"]
        relevant = q["relevant_documents"]
        if q["answerable"] != (q["category"] != "unanswerable"):
            raise ValueError(f"{ident}: несогласованная разметка answerable")
        if q["answerable"]:
            claim_ids = {c["id"] for c in claims if c["text"].strip()}
            if not claims or len(claim_ids) != len(claims) or claim_ids != {e["claim_id"] for e in evidence}:
                raise ValueError(f"{ident}: каждое утверждение требует evidence")
            if set(relevant) != {e["document_id"] for e in evidence} or len(set(relevant)) != len(relevant):
                raise ValueError(f"{ident}: документ разметки без evidence")
            if q["category"] == "multi_document" and len(relevant) < 2:
                raise ValueError(f"{ident}: нужны два или больше документов")
            for e in evidence:
                doc_id = e["document_id"]
                start, end = e["char_start"], e["char_end"]
                if (
                    snapshot["documents"][doc_id]["sha256"] != e["source_sha256"]
                    or start < 0
                    or end <= start
                    or len(e["quote"]) != end - start
                    or not e["section"].strip()
                    or not e["url"].startswith("https://cheatsheetseries.owasp.org/")
                    or not e["anchor"].strip()
                    or e["anchor"].casefold() not in e["quote"].casefold()
                    or (local_docs and local_docs[doc_id][start:end] != e["quote"])
                ):
                    raise ValueError(f"{ident}: evidence не совпадает со snapshot")
        else:
            absence = q["absence"]
            markers = absence["search_markers"]
            if (
                claims
                or evidence
                or relevant
                or not absence["reason"].strip()
                or not markers
                or any(not marker.strip() for marker in markers)
                or absence["searched_documents"] != len(snapshot["documents"])
                or absence["hits"] != 0
                or any(
                    marker.casefold() in text.casefold() for marker in markers for text in local_docs.values()
                )
            ):
                raise ValueError(f"{ident}: недостаточная проверка отсутствия")
