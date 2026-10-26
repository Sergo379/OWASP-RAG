"""Explicit download only. Inference never contacts Hugging Face."""

import hashlib
import os
import time
from pathlib import Path

import httpx

from src.core.config import AppError
from src.storage.files import now, read_json, write_json


def file_digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


class ModelFiles:
    def __init__(self, root, config, artifacts):
        self.root, self.config, self.artifacts = root, config, artifacts

    def folder(self, key: str) -> Path:
        if key not in self.config.models:
            raise AppError("Неизвестная модель")
        return self.root / key / self.config.models[key].revision

    def ready(self, key: str, verify: bool = False) -> bool:
        folder = self.folder(key)
        marker = read_json(folder / "ready.json", {})
        if marker.get("artifacts") != self.artifacts[key]:
            return False
        for item in self.artifacts[key]:
            path = folder / item["path"]
            if not path.is_file() or path.stat().st_size != item["size"]:
                return False
            if verify and file_digest(path) != item["sha256"]:
                return False
        return True

    def statuses(self) -> list[dict]:
        result = []
        for key, spec in self.config.models.items():
            folder = self.folder(key)
            state = read_json(folder / "state.json", {})
            size = 0
            if folder.exists():
                for path in folder.glob("*"):
                    try:
                        if path.is_file():
                            size += path.stat().st_size
                    except FileNotFoundError:
                        continue
            result.append(
                {
                    "key": key,
                    **spec.model_dump(),
                    "model_identity": spec.identity,
                    "status": "error"
                    if state.get("status") == "error"
                    else "ready"
                    if self.ready(key)
                    else state.get("status", "missing"),
                    "error": state.get("error"),
                    "disk_bytes": size,
                    "expected_bytes": sum(a["size"] for a in self.artifacts[key]),
                }
            )
        return result

    def prepare(self, key: str, progress=lambda **kw: None, client=None) -> dict:
        folder = self.folder(key)
        folder.mkdir(parents=True, exist_ok=True)
        own = client is None
        client = client or httpx.Client(timeout=httpx.Timeout(30, connect=10), follow_redirects=True)
        files = self.artifacts[key]
        total, done = sum(a["size"] for a in files), 0
        write_json(folder / "state.json", {"status": "downloading", "updated_at": now()})
        started = time.perf_counter()
        try:
            for index, item in enumerate(files):
                path = folder / item["path"]
                if (
                    path.exists()
                    and path.stat().st_size == item["size"]
                    and file_digest(path) == item["sha256"]
                ):
                    done += item["size"]
                    progress(
                        completed=done,
                        total=total,
                        current=item["path"],
                        files_completed=index + 1,
                        files_total=len(files),
                        units="bytes",
                    )
                    continue
                url = f"https://huggingface.co/{self.config.models[key].repository}/resolve/{self.config.models[key].revision}/{item['path']}?download=true"
                temporary = path.with_suffix(path.suffix + ".part")
                for attempt in range(3):
                    try:
                        checksum, received, last_report = hashlib.sha256(), 0, 0.0
                        with client.stream("GET", url) as response:
                            response.raise_for_status()
                            with temporary.open("wb") as stream:
                                for block in response.iter_bytes(1024 * 256):
                                    received += len(block)
                                    if received > item["size"]:
                                        raise AppError(
                                            "Размер файла модели не совпал с закреплённым snapshot"
                                        )
                                    stream.write(block)
                                    checksum.update(block)
                                    if time.perf_counter() - last_report > 0.5:
                                        progress(
                                            completed=done + received,
                                            total=total,
                                            current=item["path"],
                                            files_completed=index,
                                            files_total=len(files),
                                            units="bytes",
                                        )
                                        last_report = time.perf_counter()
                                stream.flush()
                                os.fsync(stream.fileno())
                        if received != item["size"] or checksum.hexdigest() != item["sha256"]:
                            raise AppError(
                                "SHA256/размер модели не совпал; неподтверждённые weights не используются"
                            )
                        os.replace(temporary, path)
                        break
                    except httpx.HTTPError as exc:
                        if attempt == 2:
                            raise AppError(
                                "Не удалось скачать модель: проверьте интернет и повторите Prepare Model"
                            ) from exc
                        time.sleep(2**attempt)
                done += item["size"]
            write_json(
                folder / "ready.json",
                {"identity": self.config.models[key].identity, "artifacts": files, "verified_at": now()},
            )
            write_json(folder / "state.json", {"status": "ready", "updated_at": now()})
            progress(
                completed=total,
                total=total,
                current="Файлы проверены SHA256",
                units="bytes",
                files_completed=len(files),
                files_total=len(files),
            )
            return {
                "model": key,
                "model_identity": self.config.models[key].identity,
                "disk_bytes": total,
                "download_duration_ms": round((time.perf_counter() - started) * 1000, 2),
            }
        except Exception as exc:
            message = (
                str(exc) if isinstance(exc, AppError) else "Ошибка локального кеша модели; проверьте logs"
            )
            write_json(folder / "state.json", {"status": "error", "error": message, "updated_at": now()})
            raise
        finally:
            if own:
                client.close()
