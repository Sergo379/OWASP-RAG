import asyncio
import json
import queue

from fastapi import Request
from fastapi.responses import StreamingResponse

from src.generation.config import ChatRequest


def install_routes(app, service):
    @app.get("/api/llm/status")
    def llm_status():
        return {
            **service.chat.ollama.status(),
            "config": service.chat.config.model_dump(exclude={"tokenizer"}),
        }

    @app.get("/api/observability/status")
    def observability():
        return service.chat.tracing.status()

    @app.post("/api/chat/sessions", status_code=201)
    def create_session():
        return service.chat.create_session()

    @app.get("/api/chat/sessions/{session_id}")
    def session(session_id: str):
        return service.chat.session(session_id)

    @app.post("/api/chat/sessions/{session_id}/clear")
    def clear(session_id: str):
        return service.chat.clear(session_id)

    @app.post("/api/chat", status_code=202)
    def chat(body: ChatRequest):
        return service.chat.start(body)

    @app.get("/api/chat/requests/{request_id}")
    def chat_request(request_id: str):
        return service.chat.get(request_id)

    @app.post("/api/chat/requests/{request_id}/cancel")
    def cancel(request_id: str):
        return service.chat.cancel(request_id)

    @app.post("/api/chat/stream")
    def stream(body: ChatRequest, request: Request):
        result = service.chat.start(body)
        job = service.chat.jobs[result["request_id"]]

        async def events():
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        item = await asyncio.to_thread(job["events"].get, True, 0.25)
                    except queue.Empty:
                        if job["done"].is_set():
                            # Recover even when a slow consumer overflowed its delta queue.
                            item = {"event": "final", "data": service.chat.get(result["request_id"])}
                        else:
                            yield ": heartbeat\n\n"
                            continue
                    yield (
                        "event: "
                        + item["event"]
                        + "\ndata: "
                        + json.dumps(item["data"], ensure_ascii=False)
                        + "\n\n"
                    )
                    if item["event"] == "final":
                        break
            finally:
                if job["result"]["status"] == "running":
                    service.chat.cancel(result["request_id"])

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
