"""Local experiment UI backed by the same CLI worker and on-disk checkpoints."""

import subprocess
import sys
import threading
from typing import Literal

from fastapi import HTTPException
from fastapi.responses import FileResponse, Response
from filelock import FileLock, Timeout
from pydantic import Field

from src.core.config import AppError
from src.core.models import StrictModel
from src.core.service import BusyError

from .dataset import load_dataset
from .experiments import SUITES, plan
from .metrics import MAX_SCORE
from .reporting import export_csv, export_run, results
from .reviews import review_for, save_review
from .storage import RunStore, checked


class StartRequest(StrictModel):
    suite: Literal["retrieval", "threshold", "generation", "final"]
    parent: str | None = Field(None, pattern=r"^[a-f0-9]{32}$")
    group: (
        Literal["chunking", "size", "overlap", "topk", "reranking", "final_retrieval", "final_generation"]
        | None
    ) = None


class ResumeRequest(StrictModel):
    retry_errors: bool = False


class ReviewRequest(StrictModel):
    scores: dict[str, int]
    reviewer: str = Field(min_length=1, max_length=100)
    notes: str = Field(min_length=1, max_length=4000)


def install_routes(app, service):
    root = service.settings.root
    guard = threading.Lock()
    child = None

    def repository():
        return RunStore(root)

    def check_idle(store):
        if child is not None and child.poll() is None:
            raise BusyError("Эксперимент уже выполняется")
        try:
            with FileLock(store.lock_path, timeout=0):
                pass
        except Timeout as exc:
            raise BusyError("Эксперимент уже выполняется") from exc

    def spawn(store, run_id, *, resume=False, retry_errors=False):
        nonlocal child
        args = [sys.executable, "-m", "src.evaluation.cli", "worker", "--run-id", run_id, "--offline"]
        if resume:
            args.append("--resume")
        if retry_errors:
            args.append("--retry-errors")
        with (store.path(run_id) / "worker.log").open("ab") as log:
            child = subprocess.Popen(
                args,
                cwd=root,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

        def observe(worker):
            if worker.wait() and store.state(run_id).get("status") == "queued":
                store.update(run_id, status="failed", error="Worker не запустился. См. worker.log")

        threading.Thread(target=observe, args=(child,), daemon=True).start()

    @app.get("/api/evaluation")
    def evaluation():
        data = load_dataset(root)
        store = repository()
        runs = []
        for folder in sorted(store.root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not (folder / "manifest.json").is_file():
                continue
            manifest = store.manifest(folder.name)
            runs.append(
                {
                    "id": folder.name,
                    "suite": manifest["suite"],
                    "split": manifest["split"],
                    "created_at": manifest["created_at"],
                    "planned": sum(len(c["questions"]) for c in manifest["configurations"]),
                    "state": store.state(folder.name),
                }
            )
        frozen = root / "experiments/manifests/selection.json"
        return {
            "dataset": data.manifest["dataset_version"],
            "sha256": data.manifest["sha256"],
            "splits": {name: len(data.split(name)) for name in ("development", "final")},
            "suites": SUITES,
            "runs": runs,
            "frozen": frozen.is_file(),
            "published_report": (root / "RESEARCH_REPORT.md").is_file(),
        }

    @app.post("/api/evaluation/runs", status_code=202)
    def start(request: StartRequest):
        store = repository()
        with guard:
            check_idle(store)
            dataset = load_dataset(root, service.store)
            if request.suite == "threshold" and not request.parent:
                raise AppError("Выберите development run для расчёта порогов")
            manifest = plan(
                service, dataset, store, request.suite, parent=request.parent, group=request.group
            )
            run_id = store.create({**manifest, "offline": True})["run_id"]
            spawn(store, run_id)
        return {"id": run_id, "status": "queued"}

    @app.post("/api/evaluation/runs/{run_id}/resume", status_code=202)
    def resume(run_id: str, request: ResumeRequest):
        store = repository()
        with guard:
            check_idle(store)
            store.manifest(run_id)
            spawn(store, run_id, resume=True, retry_errors=request.retry_errors)
        return {"id": run_id, "status": "queued"}

    @app.post("/api/evaluation/runs/{run_id}/stop", status_code=202)
    def stop(run_id: str):
        store = repository()
        store.stop(run_id)
        return {"id": run_id, "cancellation_requested": True}

    @app.get("/api/evaluation/runs/{run_id}")
    def run(run_id: str):
        store = repository()
        summary = results(store, run_id)
        return {
            "state": summary["state"],
            "configurations": summary["configurations"],
            "indexes": summary["indexes"],
            "tasks": [
                {
                    "key": r["key"],
                    "question_id": r["question_id"],
                    "status": r["status"],
                    "label": r["configuration_label"],
                    "trace_id": r.get("trace_id"),
                }
                for r in summary["rows"]
            ],
            "charts": [p.name for p in sorted((root / "experiments/charts" / run_id).glob("*.svg"))],
        }

    @app.get("/api/evaluation/runs/{run_id}/questions/{key}")
    def question(run_id: str, key: str):
        store = repository()
        store.manifest(run_id)
        row = store.rows(run_id).get(key)
        if row is None:
            raise HTTPException(404, "Результат вопроса не найден")
        dataset = load_dataset(root)
        label = next(q for q in dataset.questions if q["id"] == row["question_id"])
        return {
            "question": label,
            "row": row,
            "review": review_for(store, run_id, key, row),
            "rubric": MAX_SCORE,
        }

    @app.post("/api/evaluation/runs/{run_id}/questions/{key}/review")
    def review(run_id: str, key: str, request: ReviewRequest):
        return save_review(repository(), run_id, key, request.model_dump())

    @app.post("/api/evaluation/runs/{run_id}/charts")
    def charts(run_id: str):
        return export_run(root, repository(), run_id, charts=True)

    @app.get("/api/evaluation/runs/{run_id}/charts/{name}")
    def chart(run_id: str, name: str):
        repository().manifest(run_id)
        if not name.endswith(".svg") or not name.removesuffix(".svg").replace("-", "").isalnum():
            raise HTTPException(404)
        path = root / "experiments/charts" / run_id / name
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type="image/svg+xml")

    @app.get("/api/evaluation/runs/{run_id}/export/{kind}")
    def export(run_id: str, kind: Literal["csv", "json"]):
        report = results(repository(), run_id)
        filename = f"owasp-evaluation-{run_id}.{kind}"
        headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
        if kind == "csv":
            return Response(export_csv(report["rows"]), media_type="text/csv", headers=headers)
        from fastapi.responses import JSONResponse

        return JSONResponse(report, headers=headers)

    @app.get("/api/evaluation/report")
    def research_report():
        path = root / "RESEARCH_REPORT.md"
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type="text/markdown", filename=path.name)

    @app.get("/api/evaluation/selection")
    def selection():
        return checked(root / "experiments/manifests/selection.json")
