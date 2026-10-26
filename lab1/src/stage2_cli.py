"""Pixi entry points use the same services as FastAPI."""

import argparse
import json

from src.core.config import AppError, Settings
from src.core.service import Service
from src.observability.local import configure
from src.retrieval.models import SearchRequest


def main():
    parser = argparse.ArgumentParser(description="OWASP-RAG stage 2")
    parser.add_argument(
        "command", choices=["models", "prepare-model", "indexes", "build-index", "inspect-index", "search"]
    )
    parser.add_argument("--model", choices=["e5", "minilm", "reranker"], default="e5")
    parser.add_argument("--configuration-id")
    parser.add_argument("--index-id")
    parser.add_argument(
        "--heading-policy", choices=["include_all", "skip_heading_only"], default="include_all"
    )
    parser.add_argument("--overflow-policy", choices=["split", "error"], default="split")
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--query")
    parser.add_argument("--top-k", type=int, choices=[3, 5, 10, 20], default=5)
    parser.add_argument("--threshold", type=float, default=0)
    parser.add_argument("--topic")
    parser.add_argument("--document-id")
    parser.add_argument("--candidate-pool", type=int, default=80)
    parser.add_argument("--reranking", action="store_true")
    args = parser.parse_args()
    try:
        settings = Settings()
        configure(settings.root)
        service = Service(settings)
        if args.command == "models":
            result = service.model_statuses()
        elif args.command == "indexes":
            result = service.vectors.lists(service.operation())
        elif args.command == "inspect-index":
            result = service.vectors.inspect(args.index_id, service.operation())
        elif args.command == "search":
            result = service.search(
                SearchRequest(
                    query=args.query,
                    index_id=args.index_id,
                    model=args.model,
                    top_k=args.top_k,
                    similarity_threshold=args.threshold,
                    topic=args.topic,
                    document_id=args.document_id,
                    candidate_pool_size=args.candidate_pool,
                    reranking=args.reranking,
                )
            )
        else:
            options = {"model": args.model}
            if args.command == "build-index":
                options.update(
                    configuration_id=args.configuration_id,
                    heading_policy=args.heading_policy,
                    overflow_policy=args.overflow_policy,
                    rebuild=args.rebuild,
                )
            result = service.start(args.command, options, background=False)
            if result["status"] != "succeeded":
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 1
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (AppError, ValueError) as exc:
        print(f"Ошибка: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
