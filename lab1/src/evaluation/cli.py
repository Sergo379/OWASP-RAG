import argparse
import json
import socket
import sys

from src.core.config import AppError, Settings
from src.core.service import Service
from src.storage.vectors import corpus_snapshot

from .dataset import load_dataset
from .experiments import SUITES, freeze, plan
from .runner import Runner
from .storage import RunStore


def restrict_network():
    original = socket.socket.connect

    def local_only(sock, address):
        if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
            return original(sock, address)
        raise OSError("Offline evaluation: blocked non-loopback connection")

    socket.socket.connect = local_only


def print_json(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="OWASP-RAG Stage 4")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check")
    dataset_parser = commands.add_parser("dataset")
    dataset_parser.add_argument("--split", choices=["development", "final"])
    run = commands.add_parser("run")
    run.add_argument("--suite", required=True, choices=SUITES)
    run.add_argument("--parent")
    run.add_argument("--group")
    run.add_argument("--offline", action="store_true")
    resume = commands.add_parser("resume")
    resume.add_argument("--run-id", required=True)
    resume.add_argument("--retry-errors", action="store_true")
    resume.add_argument("--offline", action="store_true")
    worker = commands.add_parser("worker")
    worker.add_argument("--run-id", required=True)
    worker.add_argument("--retry-errors", action="store_true")
    worker.add_argument("--offline", action="store_true")
    worker.add_argument("--resume", action="store_true")
    stop = commands.add_parser("stop")
    stop.add_argument("--run-id", required=True)
    for name in ("report", "charts"):
        export = commands.add_parser(name)
        export.add_argument("--run-id", required=True)
    select = commands.add_parser("freeze")
    select.add_argument("--retrieval-run", required=True)
    select.add_argument("--retrieval-label", required=True)
    select.add_argument("--generation-run", required=True)
    select.add_argument("--generation-label", required=True)
    select.add_argument("--rationale", required=True)
    args = parser.parse_args()
    if getattr(args, "offline", False):
        restrict_network()
    try:
        service = Service(Settings())
        root = service.settings.root
        store = RunStore(root)
        local_verify = args.command in ("check", "dataset", "run", "resume", "worker", "freeze")
        data = load_dataset(root, service.store if local_verify else None)
        if args.command == "check":
            print_json(
                {
                    "dataset": data.manifest["dataset_version"],
                    "sha256": data.manifest["sha256"],
                    "split": {name: len(data.split(name)) for name in ("development", "final")},
                    "documents": len(corpus_snapshot(service.store.manifest())["documents"]),
                    "published_indexes": [
                        {
                            "id": record["index_id"],
                            "vectors": service.vectors.collection(record).count(),
                            "generation": record["published"]["generation"],
                            "model": record["identity"]["model"],
                        }
                        for record in service.vectors.catalog()["indexes"].values()
                        if record.get("published")
                    ],
                }
            )
        elif args.command == "dataset":
            print_json(list(data.questions if args.split is None else data.split(args.split)))
        elif args.command == "run":
            manifest = plan(service, data, store, args.suite, parent=args.parent, group=args.group)
            manifest = store.create({**manifest, "offline": args.offline})
            print(f"RUN_ID={manifest['run_id']}", flush=True)
            return Runner(service, data, store).run(manifest["run_id"])
        elif args.command in ("resume", "worker"):
            if store.manifest(args.run_id).get("offline") and not args.offline:
                restrict_network()
            return Runner(service, data, store).run(
                args.run_id, retry=args.retry_errors, resume=args.command == "resume" or args.resume
            )
        elif args.command == "stop":
            store.stop(args.run_id)
        elif args.command == "freeze":
            print_json(
                freeze(
                    service,
                    data,
                    store,
                    args.retrieval_run,
                    args.retrieval_label,
                    args.generation_run,
                    args.generation_label,
                    args.rationale,
                )
            )
        elif args.command in ("report", "charts"):
            from .reporting import export_run

            print_json(export_run(root, store, args.run_id, charts=args.command == "charts"))
        return 0
    except AppError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
