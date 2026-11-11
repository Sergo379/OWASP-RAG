"""Resolve matching published indexes or build them in the research workspace."""

import json
import time
from shutil import copyfile

from filelock import FileLock

from src.core.config import AppError, Settings
from src.core.models import PreprocessingConfig
from src.core.service import Service
from src.generation.resources import ResourceMonitor
from src.preprocessing.pipeline import preprocess
from src.retrieval.models import BuildIndexRequest
from src.storage.files import digest, read_json, write_json
from src.storage.vectors import corpus_snapshot, directory_bytes


def options(primary, parameters):
    config = PreprocessingConfig.model_validate(
        {
            **primary.settings.preprocessing.model_dump(),
            "strategy": parameters["strategy"],
            "chunk_size": parameters["size"],
            "overlap": parameters["overlap"],
        }
    )
    config_id = digest(json.dumps(config.model_dump(), sort_keys=True).encode())[:20]
    return config, BuildIndexRequest(model=parameters["model"], configuration_id=config_id)


class Indexes:
    def __init__(self, primary, dataset):
        self.primary = primary
        self.snapshot = dataset.manifest["corpus_snapshot"]
        self.root = (
            primary.settings.root
            / ".cache/evaluation/index-workspaces"
            / digest(json.dumps(self.snapshot, sort_keys=True).encode())[:16]
        )
        self.research = None
        self.resolved = {}

    def workspace(self):
        if self.research is not None:
            return self.research
        primary = self.primary
        research = Service(Settings(primary.settings.root, data_dir=self.root))
        research.models = primary.models
        research.model_files = primary.model_files
        research.lock_path = primary.lock_path
        manifest = primary.store.manifest()
        if corpus_snapshot(manifest) != self.snapshot:
            raise AppError("Корпус изменился после старта")
        with FileLock(primary.lock_path, timeout=5):
            research.store.save_manifest(manifest)
            for doc in manifest.documents.values():
                if doc.status == "active":
                    target = research.store.raw_path(doc.content_sha256)
                    if not target.exists():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        copyfile(primary.store.raw_path(doc.content_sha256), target)
            source_cache = primary.vectors.root / "embedding_cache.sqlite"
            target_cache = research.vectors.root / source_cache.name
            if source_cache.is_file() and not target_cache.exists():
                target_cache.parent.mkdir(parents=True, exist_ok=True)
                copyfile(source_cache, target_cache)
        self.research = research
        return research

    def resolve(self, parameters, progress):
        cfg, build = options(self.primary, parameters)
        key = (build.configuration_id, build.model)
        if key in self.resolved:
            return self.resolved[key]
        primary = self.primary
        for record in primary.vectors.catalog()["indexes"].values():
            ident = record["identity"]
            if (
                record.get("published")
                and ident["source_snapshot"] == self.snapshot
                and ident["preprocessing_configuration_id"] == build.configuration_id
                and ident["model"] == build.model
                and ident["embedding_configuration"]["heading_policy"] == build.heading_policy
                and ident["embedding_configuration"]["overflow_policy"] == build.overflow_policy
                and ident["model_artifacts"] == primary.vectors.artifacts[build.model]
            ):
                self.resolved[key] = (primary, self.details(primary, record, "published_reused"))
                return self.resolved[key]
        research = self.workspace()
        with FileLock(primary.lock_path, timeout=5):
            monitor = ResourceMonitor()
            monitor.start()
            start = time.perf_counter()
            preprocessing_ms = None
            try:
                if research.store.run(build.configuration_id) is None:
                    tokenizer = primary.tokenizer_factory(cfg.tokenizer, primary.store.root / "tokenizers")
                    preprocess(research.store, cfg, tokenizer, progress)
                    preprocessing_ms = (time.perf_counter() - start) * 1000
                existing_id, _, _ = research.vectors.identity(build)
                metadata_path = self.root / "builds" / f"{existing_id}.json"
                result = research.vectors.build(build, research.models, progress)
                record = research.vectors.catalog()["indexes"][result["index_id"]]
            finally:
                resources = monitor.finish()
            build_measurement = {
                **resources,
                "build_call_ms": result["duration_ms"],
                "preprocessing_ms": preprocessing_ms,
                "reused_vectors": result.get("reused_vectors", 0),
                "computed_vectors": result["computed_vectors"],
                "cache_hits": result["cache_hits"],
                "total_call_ms": (time.perf_counter() - start) * 1000,
            }
            if not result["reused"]:
                write_json(metadata_path, build_measurement)
            details = self.details(research, record, "research_index")
            details["build_measurement"] = read_json(metadata_path)
        self.resolved[key] = (research, details)
        return self.resolved[key]

    def details(self, service, record, source):
        pub = record["published"]
        folder = service.vectors.root / record["index_id"]
        sidecar = read_json(folder / f"g{pub['generation']}.json")
        return {
            "index_id": record["index_id"],
            "source": source,
            "published": pub,
            "identity": record["identity"],
            "disk_bytes": directory_bytes(folder),
            "analysis": sidecar["analysis"],
            "build_measurement": None,
        }


def ensure_qwen_tokenizer(primary, service):
    if service is not primary and service.chat.tokenizer is None:
        service.chat.tokenizer = primary.chat.prepare_tokenizer()
