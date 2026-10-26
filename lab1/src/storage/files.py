"""Immutable blobs plus atomic pointers: interrupted writes cannot corrupt old state."""

import hashlib
import json
import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from src.core.config import AppError
from src.core.models import Manifest


def now() -> str:
    return datetime.now(UTC).isoformat()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".write-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # Windows readers/antivirus can briefly hold a handle without FILE_SHARE_DELETE.
        # Preserve atomic replacement and retry only this transient sharing failure.
        for attempt in range(8):
            try:
                os.replace(name, path)
                break
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(min(0.005 * 2**attempt, 0.1))
    finally:
        if os.path.exists(name):
            os.unlink(name)


def write_json(path: Path, value: dict) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AppError(
            f"Повреждён файл {path.name}. Сохраните копию и восстановите из резервной копии."
        ) from exc


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.manifest_path = self.root / "manifests/manifest.json"
        self.root.mkdir(parents=True, exist_ok=True)

    def manifest(self) -> Manifest:
        try:
            return Manifest.model_validate(read_json(self.manifest_path, {}))
        except ValidationError as exc:
            raise AppError("Некорректный manifest.json; файл не перезаписан.") from exc

    def save_manifest(self, manifest: Manifest) -> None:
        write_json(self.manifest_path, manifest.model_dump())

    def raw_path(self, sha: str) -> Path:
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise AppError("Некорректный SHA256")
        return self.root / "raw" / f"{sha}.md"

    def raw(self, sha: str) -> bytes:
        try:
            data = self.raw_path(sha).read_bytes()
        except OSError as exc:
            raise AppError("Исходный документ отсутствует; повторите Grabber") from exc
        if digest(data) != sha:
            raise AppError("Нарушена целостность raw; повторите Grabber")
        return data

    def processed_index(self) -> dict:
        return read_json(self.root / "processed/index.json", {"runs": {}, "latest": None})

    def run(self, configuration_id: str | None = None) -> dict | None:
        index = self.processed_index()
        return index["runs"].get(configuration_id or index["latest"])

    def chunks(self, run: dict):
        # The filename is derived from a hash, never from downloaded document paths.
        name = run["file"]
        if Path(name).name != name:
            raise AppError("Некорректный processed index")
        with (self.root / "processed" / name).open(encoding="utf-8") as stream:
            for line in stream:
                yield json.loads(line)
