"""One-factor configurations and immutable study manifests."""

import importlib.metadata
import platform
import sys

from src.core.config import AppError, load_yaml
from src.generation.config import load_generation
from src.storage.files import digest, now

from .metrics import percentile
from .storage import checked, identity, save

SUITES = ("retrieval", "threshold", "generation", "final")


def configuration(label, family, params):
    return {"label": label, "family": family, "id": identity(params)[:16], "parameters": params}


def normalized_checksums(root, checksums):
    return {path: digest((root / path).read_bytes().replace(b"\r\n", b"\n")) for path in checksums}


def protocol(service):
    root = service.settings.root
    config = load_yaml(root / "configs/evaluation.yaml")
    generation, _ = load_generation(root)
    components = ("preprocessing", "embeddings", "retrieval", "generation")
    files = [
        root / "configs" / name
        for name in (
            "preprocessing.yaml",
            "retrieval.yaml",
            "generation.yaml",
            "prompts.yaml",
            "model_artifacts.json",
        )
    ]
    for component in components:
        files.extend(sorted((root / "src" / component).glob("*.py")))
    files.append(root / "src/storage/vectors.py")
    return {
        "config": config,
        "generation_config": generation.model_dump(),
        "runtime": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "cpu": platform.processor(),
            "packages": {
                name: importlib.metadata.version(name)
                for name in ("chromadb", "torch", "transformers", "langfuse", "opentelemetry-sdk", "numpy")
            },
        },
        "model_artifacts": service.vectors.artifacts,
        "checksums": {path.relative_to(root).as_posix(): digest(path.read_bytes()) for path in files},
    }


def threshold_plan(store, parent, settings):
    manifest = store.manifest(parent)
    if manifest["suite"] != "retrieval" or manifest["split"] != "development":
        raise AppError("Для thresholds нужен development run retrieval")
    rows = store.rows(parent).values()
    output = {}
    for model in ("e5", "minilm"):
        group = [
            r for r in rows if r["configuration_label"] == f"topk-{model}-5" and r["status"] == "completed"
        ]
        if len(group) != 10 or any(not r["candidates"] for r in group):
            raise AppError("Сначала завершите 10 baseline поисков для каждой embedding-модели")
        maxima = [max(c["vector_similarity"] for c in r["candidates"]) for r in group]
        levels = [round(percentile(maxima, q), 6) for q in settings["selection"]["threshold_quantiles"]]
        output[model] = {
            "top_score_by_question": dict(zip([r["question_id"] for r in group], maxima, strict=True)),
            "quantiles": levels,
        }
    return output


def plan(service, dataset, store, suite, parent=None, selected=None, group=None):
    environment = protocol(service)
    settings = environment["config"]
    base = settings["base"]
    planned = []
    extra = {}
    allowed_questions = dataset.split("final" if suite == "final" else "development")

    def add(label, family, changes=None, questions=None):
        chosen = (
            allowed_questions if questions is None else [q for q in allowed_questions if q["id"] in questions]
        )
        if questions is not None and len(chosen) != len(questions):
            raise AppError("Список сравнения выходит за пределы split")
        planned.append(
            {
                **configuration(label, family, {**base, **(changes or {})}),
                "questions": [q["id"] for q in chosen],
            }
        )

    if suite == "retrieval":
        for strategy, overlap in (("fixed", 0), ("overlap", 0.1), ("paragraph", 0), ("section", 0)):
            add("chunk-" + strategy, "chunking", {"strategy": strategy, "overlap": overlap})
        for size in (128, 256, 384):
            add(f"size-{size}", "size", {"size": size})
        for overlap in (0.0, 0.1, 0.2):
            add(f"overlap-{int(overlap * 100)}", "overlap", {"strategy": "overlap", "overlap": overlap})
        for model in ("e5", "minilm"):
            for k in (3, 5, 10, 20):
                add(f"topk-{model}-{k}", "topk", {"model": model, "k": k})
        add("rerank-off", "reranking", {"pool": 20})
        add("rerank-on", "reranking", {"pool": 20, "rerank": True})
        if group:
            planned = [c for c in planned if c["family"] == group]
            if not planned:
                raise AppError("Неизвестная группа retrieval")
    elif suite == "threshold":
        distribution = threshold_plan(store, parent, settings)
        extra = {"parent_run_id": parent, "threshold_calibration": distribution}
        for model, data in distribution.items():
            for label, threshold in zip(
                ("off", "low", "medium", "strict"), [-1.0, *data["quantiles"]], strict=True
            ):
                add(f"threshold-{model}-{label}", "threshold", {"model": model, "threshold": threshold})
    elif suite == "generation":
        gen = settings["generation"]
        fixed = {
            "llm": gen["base_model"],
            "mode": gen["base_prompt"],
            "answer_format": gen["answer_format"],
            "retrieval_strategy": gen["retrieval_strategy"],
        }
        add("qwen-17", "llm", fixed, gen["questions"])
        add("qwen-4-instruct", "llm", {**fixed, "llm": gen["comparison_model"]}, gen["questions"])
        add("prompt-concise", "prompt", {**fixed, "mode": gen["comparison_prompt"]}, gen["prompt_questions"])
    elif suite == "final":
        selection = checked(service.settings.root / "experiments/manifests/selection.json")
        comparable_checksums = normalized_checksums(service.settings.root, environment["checksums"])
        if selection["dataset_sha256"] != dataset.manifest["sha256"] or selection.get(
            "normalized_checksums", selection["checksums"]
        ) not in (comparable_checksums, environment["checksums"]):
            raise AppError("Конфигурация final заморожена для другого snapshot или pipeline")
        if selected and selected != selection["id"]:
            raise AppError("ID зафиксированной конфигурации не совпал")
        extra = {"selection": selection}
        add("final-retrieval", "final_retrieval", selection["retrieval"])
        add("final-generation", "final_generation", {**selection["retrieval"], **selection["generation"]})
        if group:
            if group not in ("final_retrieval", "final_generation"):
                raise AppError("Неизвестная группа final")
            planned = [c for c in planned if c["family"] == group]
    else:
        raise AppError("Неизвестный suite")
    return {
        "schema_version": 1,
        "suite": suite,
        "split": "final" if suite == "final" else "development",
        "dataset_sha256": dataset.manifest["sha256"],
        "dataset_version": dataset.manifest["dataset_version"],
        "source_snapshot": dataset.manifest["corpus_snapshot"],
        "environment": environment,
        "configurations": planned,
        **extra,
    }


def freeze(
    service, dataset, store, retrieval_id, retrieval_label, generation_id, generation_label, rationale
):
    target = service.settings.root / "experiments/manifests/selection.json"
    if target.exists():
        raise AppError("Выбор уже заморожен")
    records = []
    checksums = protocol(service)["checksums"]
    for run_id, label in ((retrieval_id, retrieval_label), (generation_id, generation_label)):
        manifest = store.manifest(run_id)
        configs = [c for c in manifest["configurations"] if c["label"] == label]
        results = [r for r in store.rows(run_id).values() if r["configuration_label"] == label]
        if (
            manifest["split"] != "development"
            or manifest["dataset_sha256"] != dataset.manifest["sha256"]
            or manifest["environment"]["checksums"] != checksums
            or len(configs) != 1
            or len(results) != len(configs[0]["questions"])
            or not any(r["status"] == "completed" for r in results)
        ):
            raise AppError("Выбор требует сохранённых результатов development и совпадающего pipeline")
        records.append(configs[0]["parameters"])
    retrieval, generation = records
    fields = {"llm", "mode", "answer_format", "retrieval_strategy"}
    if fields.intersection(retrieval) or not fields.issubset(generation) or not rationale.strip():
        raise AppError("Укажите измеренные retrieval/generation и обоснование")
    selection = {
        "created_at": now(),
        "dataset_sha256": dataset.manifest["sha256"],
        "checksums": checksums,
        "normalized_checksums": normalized_checksums(service.settings.root, checksums),
        "retrieval": retrieval,
        "generation": {key: generation[key] for key in sorted(fields)},
        "development_runs": [retrieval_id, generation_id],
        "development_configs": [retrieval_label, generation_label],
        "rationale": rationale.strip(),
    }
    selection["id"] = identity(selection)[:16]
    save(target, selection)
    return selection
