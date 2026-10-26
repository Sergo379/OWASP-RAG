import argparse
import json
import sys

from fastapi.testclient import TestClient

from src.core.config import AppError, Settings
from src.core.service import Service
from src.observability.local import configure


def main():
    parser = argparse.ArgumentParser(description="OWASP-RAG stage 1")
    parser.add_argument("command", choices=["grab", "preprocess", "stats", "smoke", "tokenizer"])
    parser.add_argument("--strategy", choices=["fixed", "overlap", "paragraph", "section"])
    parser.add_argument("--chunk-size", type=int, choices=[128, 256, 384])
    parser.add_argument("--overlap", type=float, choices=[0, 0.1, 0.2])
    parser.add_argument("--network", action="store_true", help="Optional live GitHub discovery smoke")
    args = parser.parse_args()
    try:
        settings = Settings()
        configure(settings.root)
        service = Service(settings)
        if args.command == "stats":
            result = service.stats()
        elif args.command == "smoke":
            from src.api.app import create_app

            with TestClient(create_app(service)) as client:
                for route in ("/api/health", "/api/state", "/api/stats", "/api/documents", "/"):
                    response = client.get(route)
                    if response.status_code != 200:
                        raise AppError(f"Smoke failed: {route}")
            result = {"local": "passed", "network": "not requested"}
            if args.network:
                from src.grabber.github import GitHubGrabber

                grabber = GitHubGrabber(settings.corpus, service.store)
                try:
                    commit, files = grabber.discover()
                    missing = [d.path for d in settings.corpus.documents if d.path not in files]
                    if missing:
                        raise AppError(f"Missing configured documents: {missing}")
                    result.update(
                        network="passed",
                        source_commit=commit,
                        configured_documents=len(settings.corpus.documents),
                    )
                finally:
                    grabber.client.close()
        else:
            options = {
                key: value
                for key, value in {
                    "strategy": args.strategy,
                    "chunk_size": args.chunk_size,
                    "overlap": args.overlap,
                }.items()
                if value is not None
            }
            result = service.start(args.command, options, background=False)
            if result["status"] != "succeeded":
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 1
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except AppError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
