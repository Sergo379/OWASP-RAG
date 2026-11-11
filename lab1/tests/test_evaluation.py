import json
import xml.etree.ElementTree as ET
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.config import AppError
from src.evaluation.api import install_routes
from src.evaluation.dataset import load_dataset, verify
from src.evaluation.experiments import configuration, threshold_plan
from src.evaluation.metrics import aggregate, generation_scores, ranking
from src.evaluation.reporting import export_run, paired
from src.evaluation.reviews import save_review
from src.evaluation.storage import RunStore, identity
from src.storage.files import write_json

ROOT = Path(__file__).resolve().parents[1]


def test_frozen_dataset_and_claim_integrity():
    data = load_dataset(ROOT)
    assert len(data.split("development")) == 10
    assert len(data.split("final")) == 30
    assert sum(q["answerable"] for q in data.questions) == 32
    rows = deepcopy(data.questions)
    rows[0]["source_evidence"][0]["char_end"] += 1
    with pytest.raises(ValueError, match="evidence"):
        verify(rows, data.manifest, None)
    rows = deepcopy(data.questions)
    rows[0]["split"] = "final"
    with pytest.raises(ValueError, match="split"):
        verify(rows, data.manifest, None)


def test_document_ranking_counts_duplicate_as_rank_slot_not_second_relevant_document():
    rows = [{"document_id": key} for key in ["x", "a", "a", "b"]]
    assert ranking(rows, {"a", "b"}, 3) == {
        "returned": 3,
        "repeated_documents": 1,
        "relevant_chunk_share": 2 / 3,
        "recall": 0.5,
        "precision": 1 / 3,
        "mrr": 0.5,
        "hit_rate": 1,
    }
    assert ranking(rows, {"a", "b"}, 5)["recall"] == 1
    assert ranking([], {"a"}, 3)["mrr"] == 0
    assert ranking(rows, set(), 3)["recall"] is None


@pytest.mark.parametrize(
    ("status", "answerable", "outcome", "accuracy"),
    [
        ("completed", True, "answer_pending_review", 1.0),
        ("completed", False, "unsupported_answer", 0.0),
        ("refused", True, "false_refusal", 0.0),
        ("refused", False, "correct_refusal", 1.0),
        ("failed", True, None, None),
    ],
)
def test_refusal_and_semantic_review_have_separate_status(status, answerable, outcome, accuracy):
    scores = generation_scores(
        {
            "status": status,
            "diagnostics": {"validation": {"used_source_ids": ["S1"]}},
        },
        {"answerable": answerable},
    )
    assert (scores["refusal_outcome"], scores["refusal_accuracy"]) == (outcome, accuracy)
    assert "faithfulness" not in scores
    if status == "completed":
        assert scores["review_status"] == "NOT_REVIEWED"


def test_aggregate_excludes_missing_metrics_and_counts_failures():
    result = aggregate(
        [
            {"status": "completed", "scores": {"recall": 0.5}, "latency_ms": 10},
            {"status": "completed", "scores": {"recall": None}, "latency_ms": 30},
            {"status": "timeout", "scores": {}},
        ]
    )
    assert result["metrics"]["recall"] == {"mean": 0.5, "n": 1}
    assert result["metrics"]["latency_ms"]["median"] == 20
    assert result["status"]["timeout"] == 1


def test_configuration_id_stable_and_only_parameters_define_identity():
    a = configuration("a", "topk", {"k": 5, "model": "e5"})
    b = configuration("b", "chunking", {"model": "e5", "k": 5})
    assert a["id"] == b["id"]
    assert a["id"] != configuration("a", "topk", {"k": 10, "model": "e5"})["id"]
    assert identity({"b": 1, "a": 2}) == identity({"a": 2, "b": 1})


def test_atomic_attempts_keep_errors_reject_duplicates_and_detect_corruption(tmp_path):
    store = RunStore(tmp_path)
    run = store.create({"suite": "retrieval"})["run_id"]
    key = "topk-e5-5__OWASP-Q01"
    store.checkpoint(run, key, {"status": "timeout"})
    with pytest.raises(AppError, match="существует"):
        store.checkpoint(run, key, {"status": "completed"})
    store.stop(run)
    assert store.stopped(run)
    store.clear_stop(run)
    store.checkpoint(run, key, {"status": "completed"}, retry=True)
    assert store.rows(run)[key]["attempt"] == 2
    with pytest.raises(AppError, match="существует"):
        store.checkpoint(run, key, {"status": "completed"}, retry=True)
    first = store.path(run) / "rows" / key / "000001.json"
    assert first.exists()
    path = store.path(run) / "rows" / key / "000002.json"
    data = json.loads(path.read_bytes())
    data["data"]["status"] = "failed"
    write_json(path, data)
    with pytest.raises(AppError, match="целостность"):
        store.rows(run)


def test_recover_interrupted_state_from_released_lock(tmp_path):
    store = RunStore(tmp_path)
    run = store.create({"suite": "retrieval"})["run_id"]
    store.update(run, status="running", saved=2)
    assert store.state(run)["status"] == "interrupted"
    assert store.state(run)["saved"] == 2


def test_threshold_calibration_requires_full_development_data(tmp_path):
    store = RunStore(tmp_path)
    run = store.create({"suite": "retrieval", "split": "development"})["run_id"]
    with pytest.raises(AppError, match="10 baseline"):
        threshold_plan(store, run, {"selection": {"threshold_quantiles": [0.25, 0.5, 0.75]}})


def fixture_run(store, *, generation=False):
    params = {"k": 5, "llm": "local"} if generation else {"k": 5}
    run = store.create(
        {
            "suite": "generation" if generation else "retrieval",
            "split": "development",
            "configurations": [
                {
                    **configuration("topk-e5-5", "llm" if generation else "topk", params),
                    "questions": ["OWASP-Q01"],
                }
            ],
        }
    )["run_id"]
    key = "topk-e5-5__OWASP-Q01"
    store.checkpoint(
        run,
        key,
        {
            "run_id": run,
            "configuration_label": "topk-e5-5",
            "question_id": "OWASP-Q01",
            "language": "ru",
            "category": "factual",
            "difficulty": "easy",
            "answerable": True,
            "status": "completed",
            "parameters": params,
            "scores": {"recall": 0.5},
            "answer": "=Some unsafe cell",
            "latency_ms": 42,
            "request_id": "abc",
        },
    )
    store.update(run, status="completed", saved=1)
    return run, key


def test_export_csv_json_and_parseable_svg(tmp_path):
    store = RunStore(tmp_path)
    run, _ = fixture_run(store)
    out = export_run(tmp_path, store, run, charts=True)
    assert json.loads(Path(out["json"]).read_bytes())["rows"][0]["scores"]["recall"] == 0.5
    assert "'=Some unsafe cell" in Path(out["csv"]).read_text(encoding="utf-8-sig")
    assert "recall-topk.svg" in out["charts"]
    for name in out["charts"]:
        doc = ET.parse(tmp_path / "experiments/charts" / run / name)
        assert doc.getroot().tag.endswith("svg")


def test_review_requires_all_scores_and_stays_attached_to_attempt(tmp_path):
    store = RunStore(tmp_path)
    run, key = fixture_run(store, generation=True)
    with pytest.raises(AppError, match="рубрике"):
        save_review(store, run, key, {"scores": {"correctness": 2}})
    review = save_review(
        store,
        run,
        key,
        {
            "reviewer": "Reviewer",
            "notes": "Evidence confirmed",
            "scores": {
                "correctness": 2,
                "faithfulness": 2,
                "completeness": 2,
                "citation_support": 2,
                "technical_detail": 3,
            },
        },
    )
    assert review["request_id"] == "abc"
    assert review["attempt"] == 1


def test_context_pair_comparison_keeps_mismatches_visible():
    rows = [
        {"configuration_label": "a", "question_id": "q", "status": "completed", "context_fingerprint": "a"},
        {"configuration_label": "b", "question_id": "q", "status": "timeout", "context_fingerprint": "b"},
    ]
    assert paired(rows, "a", "b")[0]["same_context"] is False


def test_api_dashboard_and_inspector_use_frozen_data(tmp_path):
    target = tmp_path / "experiments/datasets"
    target.mkdir(parents=True)
    for path in (ROOT / "experiments/datasets").glob("*.json"):
        (target / path.name).write_bytes(path.read_bytes())
    store = RunStore(tmp_path)
    run, key = fixture_run(store)
    app = FastAPI()
    install_routes(app, SimpleNamespace(settings=SimpleNamespace(root=tmp_path)))
    with TestClient(app) as client:
        dash = client.get("/api/evaluation").json()
        assert dash["splits"] == {"development": 10, "final": 30}
        assert dash["runs"][0]["state"]["saved"] == 1
        assert client.get(f"/api/evaluation/runs/{run}/questions/{key}").json()["question"]["source_evidence"]
        assert (
            client.get(f"/api/evaluation/runs/{run}/export/csv")
            .headers["content-type"]
            .startswith("text/csv")
        )
        assert client.post("/api/evaluation/runs", json={"suite": "unknown"}).status_code == 422
        assert client.get(f"/api/evaluation/runs/{run}/questions/absent").status_code == 404


def test_vanilla_ui_connects_dashboard_and_inspector():
    page = (ROOT / "src/ui/index.html").read_text(encoding="utf-8")
    assert 'id="experiments"' in page and "/static/evaluation.js" in page
    for suffix in ("suite", "stop", "resume", "question", "figures", "csv", "json"):
        assert f'id="eval-{suffix}"' in page
