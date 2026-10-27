import argparse
import json
import sys

from filelock import FileLock, Timeout

from src.core.config import AppError, Settings
from src.core.service import Service
from src.generation.config import ChatRequest
from src.generation.ollama import Cancellation
from src.observability.local import configure
from src.retrieval.models import SearchRequest
from src.storage.files import write_json


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Stage 3 local RAG / Ollama / Langfuse")
    parser.add_argument("command", choices=["llm-check", "rag", "langfuse-check"])
    parser.add_argument("--query", default="How can SQL injection be prevented?")
    parser.add_argument("--index")
    parser.add_argument("--model", choices=["e5", "minilm"], default="e5")
    parser.add_argument(
        "--llm", choices=["qwen3:1.7b", "qwen3:4b", "qwen3:4b-instruct-2507-q4_K_M"], default="qwen3:1.7b"
    )
    parser.add_argument(
        "--prompt-mode", choices=["strict_grounded", "concise_grounded"], default="strict_grounded"
    )
    parser.add_argument("--reranking", action="store_true")
    parser.add_argument("--inference", action="store_true", help="Actually run each Qwen model, sequentially")
    parser.add_argument(
        "--prepare-tokenizer", action="store_true", help="Prepare the pinned Qwen tokenizer for offline RAG"
    )
    parser.add_argument(
        "--output", help="Explicit local JSON report path (can contain the requested query/answer)"
    )
    args = parser.parse_args()
    settings = Settings()
    configure(settings.root)
    service = Service(settings)
    exit_code = 0
    try:
        chat = service.chat
        args.llm = args.llm or chat.config.default_model
        if args.command == "llm-check":
            output = chat.ollama.status()
            if args.inference or args.prepare_tokenizer:
                output["tokenizer"] = chat.prepare_tokenizer().identity
            if args.inference:
                with FileLock(service.lock_path, timeout=0):
                    service.models.unload()
                    output["inference"] = []
                    for model in chat.config.models:
                        answer, metrics = chat.ollama.stream(
                            model,
                            [{"role": "user", "content": "Reply with the single word READY."}],
                            Cancellation(),
                            lambda _: None,
                        )
                        output["inference"].append({"model": model, "answer": answer, **metrics})
            if not output["available"] or not all(m["ready"] for m in output["models"]):
                exit_code = 1
        elif args.command == "langfuse-check":
            output = chat.tracing.check()
            if output["configured"] and output["cloud_verification"] == "authenticated":
                trace = chat.tracing.request("0" * 32, {"purpose": "explicit connectivity check"})
                with trace.step("LANGFUSE_CONNECTIVITY_CHECK"):
                    pass
                trace.close("completed")
                chat.tracing.flush()
                output = {
                    **chat.tracing.status(),
                    "trace_id": trace.trace_id,
                    "trace_url": chat.tracing.trace_url(trace.trace_id),
                }
            exit_code = (
                0
                if output["cloud_verification"] == "authenticated" and output["last_export"] == "accepted"
                else 2
            )
        else:
            index_id = args.index
            if not index_id:
                ready = [
                    i
                    for i in service.vectors.catalog()["indexes"].values()
                    if i.get("published") and i["identity"]["model"] == args.model
                ]
                if not ready:
                    raise AppError("Нет готового индекса выбранной embedding модели")
                index_id = ready[0]["index_id"]
            session = chat.create_session()
            request = ChatRequest(
                session_id=session["session_id"],
                llm=args.llm,
                prompt_mode=args.prompt_mode,
                retrieval=SearchRequest(
                    query=args.query, index_id=index_id, model=args.model, reranking=args.reranking
                ),
            )
            job = chat.start(request)
            output = chat.wait(job["request_id"])
            exit_code = 0 if output["status"] in ("completed", "refused") else 1
        if args.output:
            from pathlib import Path

            write_json(Path(args.output), output)
        print(
            json.dumps(
                output
                if not args.output
                else {
                    "report": args.output,
                    "status": output.get("status"),
                    "request_id": output.get("request_id"),
                    "available": output.get("available"),
                    "message": output.get("message"),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    except (AppError, Timeout) as exc:
        print(
            json.dumps(
                {"error": str(exc) if isinstance(exc, AppError) else "Ресурсы заняты"}, ensure_ascii=False
            )
        )
        exit_code = 1
    finally:
        if service._chat:
            service.chat.tracing.shutdown()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
