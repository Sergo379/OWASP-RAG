import logging

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from src import __version__
from src.core.config import AppError, Settings
from src.core.models import PreprocessRequest
from src.core.service import BusyError, Service
from src.observability.local import configure


def create_app(service: Service | None = None) -> FastAPI:
    if service is None:
        settings = Settings()
        configure(settings.root)
        service = Service(settings)
    app = FastAPI(title="OWASP-RAG", version=__version__)
    app.state.service = service
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    @app.middleware("http")
    async def same_origin(request: Request, call_next):
        if request.method == "POST" and request.headers.get("origin") not in (
            None,
            "http://127.0.0.1:8000",
            "http://localhost:8000",
        ):
            return JSONResponse(status_code=403, content={"error": "Недопустимый Origin"})
        return await call_next(request)

    @app.exception_handler(AppError)
    async def app_error(request, exc):
        return JSONResponse(
            status_code=409 if isinstance(exc, BusyError) else 400, content={"error": str(exc)}
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        logging.getLogger("owasp.api").error("API failure: %s", type(exc).__name__)
        return JSONResponse(
            status_code=500, content={"error": "Внутренняя ошибка. Проверьте технические логи."}
        )

    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": __version__}

    @app.get("/api/state")
    def state():
        return service.state()

    @app.get("/api/stats")
    def stats():
        return service.stats()

    @app.get("/api/errors")
    def errors():
        return {"items": service.errors()}

    @app.get("/api/documents")
    def documents():
        return {"items": service.documents()}

    @app.get("/api/documents/{document_id}")
    def document(document_id: str):
        doc = service.store.manifest().documents.get(document_id)
        if doc is None:
            raise HTTPException(404, "Документ не найден")
        return {"metadata": doc.model_dump(), "text": service.store.raw(doc.content_sha256).decode("utf-8")}

    @app.get("/api/documents/{document_id}/chunks")
    def chunks(
        document_id: str,
        configuration_id: str | None = None,
        offset: int = Query(0, ge=0),
        limit: int = Query(20, ge=1, le=100),
    ):
        doc = service.store.manifest().documents.get(document_id)
        if doc is None:
            raise HTTPException(404, "Документ не найден")
        run = service.store.run(configuration_id)
        if configuration_id and not run:
            raise HTTPException(404, "Конфигурация не найдена")
        if not run:
            return {"items": [], "total": 0, "stale": False}
        summary = run["documents"].get(document_id)
        stale = bool(summary and (summary["content_sha256"] != doc.content_sha256 or doc.status != "active"))
        from itertools import islice

        items = list(
            islice(
                (c for c in service.store.chunks(run) if c["document_id"] == document_id),
                offset,
                offset + limit,
            )
        )
        return {"items": items, "total": summary["chunks"] if summary else 0, "stale": stale}

    @app.post("/api/operations/grab", status_code=202)
    def grab():
        return service.start("grab")

    @app.post("/api/operations/preprocess", status_code=202)
    def process(options: PreprocessRequest):
        return service.start("preprocess", options.model_dump())

    @app.get("/api/operations/current")
    def operation():
        return {"operation": service.operation()}

    @app.get("/api/operations/{operation_id}")
    def operation_by_id(operation_id: str):
        op = service.operation()
        if op and op["operation_id"] == operation_id:
            return op
        from src.storage.files import read_json

        for old in read_json(service.store.root / "manifests/history.json", []):
            if old["operation_id"] == operation_id:
                return old
        raise HTTPException(404, "Операция не найдена")

    ui = service.settings.root / "src/ui"
    app.mount("/static", StaticFiles(directory=ui), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(ui / "index.html")

    return app
