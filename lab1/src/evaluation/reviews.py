"""Explicit reviewer scores. Automatic evaluation never writes this data."""

from src.core.config import AppError
from src.storage.files import now

from .metrics import MAX_SCORE
from .storage import checked, save


def save_review(store, run_id, key, data):
    row = store.rows(run_id).get(key)
    if not row or "llm" not in row["parameters"] or row["status"] != "completed":
        raise AppError("Разметка доступна для завершённого ответа модели")
    scores = data.get("scores", {})
    if (
        not isinstance(scores, dict)
        or set(scores) != set(MAX_SCORE)
        or any(
            type(score) is not int or not 0 <= score <= MAX_SCORE[field] for field, score in scores.items()
        )
    ):
        raise AppError("Заполните все поля по рубрике. Допустимы только целые баллы")
    reviewer = data.get("reviewer", "").strip()
    notes = data.get("notes", "").strip()
    if not reviewer or not notes or len(reviewer) > 100 or len(notes) > 4000:
        raise AppError("Укажите проверяющего и комментарий к оценке")
    record = {
        "scores": scores,
        "reviewer": reviewer,
        "notes": notes,
        "created_at": now(),
        "request_id": row["request_id"],
        "attempt": row["attempt"],
    }
    save(store.path(run_id) / "reviews" / f"{key}.json", record)
    return record


def review_for(store, run_id, key, row):
    path = store.path(run_id) / "reviews" / f"{key}.json"
    if not path.exists():
        return None
    review = checked(path)
    return (
        review
        if review["request_id"] == row.get("request_id") and review["attempt"] == row["attempt"]
        else None
    )
