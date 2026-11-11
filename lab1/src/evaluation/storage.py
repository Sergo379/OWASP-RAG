"""Atomic checkpoint per question/attempt, with immutable manifest and integrity checks."""

import json
import re
import uuid

from filelock import FileLock, Timeout

from src.core.config import AppError
from src.storage.files import digest, now, read_json, write_json

DONE = {"completed", "refused"}
RETRY = {"failed", "timeout", "cancelled", "interrupted"}
SAFE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,100}$")


def identity(value):
    return digest(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())


def checked(path):
    item = read_json(path)
    if (
        not isinstance(item, dict)
        or set(item) != {"sha256", "data"}
        or item["sha256"] != identity(item["data"])
    ):
        raise AppError(f"Нарушена целостность {path.name}")
    return item["data"]


def save(path, value):
    write_json(path, {"sha256": identity(value), "data": value})


class RunStore:
    def __init__(self, root):
        self.root = root / "experiments/runs"
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.root / ".worker.lock"

    def path(self, run_id):
        if not re.fullmatch(r"[a-f0-9]{32}", run_id):
            raise AppError("Некорректный run ID")
        return self.root / run_id

    def create(self, manifest):
        run_id = uuid.uuid4().hex
        folder = self.path(run_id)
        manifest = {**manifest, "run_id": run_id, "created_at": now()}
        save(folder / "manifest.json", manifest)
        self.update(run_id, status="queued", current=None)
        return manifest

    def manifest(self, run_id):
        manifest = checked(self.path(run_id) / "manifest.json")
        if manifest["run_id"] != run_id:
            raise AppError("Run ID не совпал с манифестом")
        return manifest

    def update(self, run_id, **state):
        write_json(self.path(run_id) / "state.json", {**state, "updated_at": now()})

    def state(self, run_id):
        path = self.path(run_id)
        state = read_json(path / "state.json", {})
        if state.get("status") == "running":
            try:
                with FileLock(self.lock_path, timeout=0):
                    state = read_json(path / "state.json", {})
                    if state.get("status") == "running":
                        state = {**state, "status": "interrupted", "current": None}
                        self.update(run_id, **state)
            except Timeout:
                pass
        return state

    def stop(self, run_id):
        self.manifest(run_id)
        write_json(self.path(run_id) / "stop.json", {"requested_at": now()})

    def stopped(self, run_id):
        return (self.path(run_id) / "stop.json").exists()

    def clear_stop(self, run_id):
        (self.path(run_id) / "stop.json").unlink(missing_ok=True)

    def checkpoint(self, run_id, key, row, retry=False):
        if not SAFE.fullmatch(key):
            raise AppError("Некорректный ключ checkpoint")
        folder = self.path(run_id) / "rows" / key
        prior = sorted(folder.glob("*.json"))
        if prior:
            latest = checked(prior[-1])
            if not retry or latest["status"] not in RETRY:
                raise AppError("Checkpoint уже существует")
        save(folder / f"{len(prior) + 1:06d}.json", {**row, "key": key, "attempt": len(prior) + 1})

    def rows(self, run_id):
        folder = self.path(run_id) / "rows"
        return (
            {
                sub.name: checked(sorted(sub.glob("*.json"))[-1])
                for sub in sorted(folder.iterdir())
                if sub.is_dir() and any(sub.glob("*.json"))
            }
            if folder.exists()
            else {}
        )

    def audit(self, run_id):
        rows = self.rows(run_id)
        for key, latest in rows.items():
            directory = self.path(run_id) / "rows" / key
            attempts = sorted(directory.glob("*.json"))
            if len(attempts) != latest["attempt"]:
                raise AppError("Нарушена последовательность попыток")
            for attempt, path in enumerate(attempts, 1):
                record = checked(path)
                if record["key"] != key or record["attempt"] != attempt or path.stem != f"{attempt:06d}":
                    raise AppError("Нарушена последовательность попыток")
        return rows

    def list(self):
        return [
            {
                "manifest": self.manifest(path.name),
                "state": self.state(path.name),
                "saved": len(self.rows(path.name)),
            }
            for path in sorted(self.root.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True)
            if path.is_dir()
            and re.fullmatch(r"[a-f0-9]{32}", path.name)
            and (path / "manifest.json").is_file()
        ]
