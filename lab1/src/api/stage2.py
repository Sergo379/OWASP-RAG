from fastapi import APIRouter, HTTPException

from src.retrieval.models import (
    BuildIndexRequest,
    IndexResponse,
    ItemsResponse,
    OperationResponse,
    PrepareModelRequest,
    SearchRequest,
    SearchResponse,
)


def install_routes(app, service):
    router = APIRouter(prefix="/api", tags=["Semantic retrieval"])

    @router.get("/embeddings/models", response_model=ItemsResponse)
    def models():
        return {"items": service.model_statuses()}

    @router.post("/embeddings/prepare", status_code=202, response_model=OperationResponse)
    def prepare(options: PrepareModelRequest):
        return service.start("prepare-model", options.model_dump())

    @router.get("/indexes", response_model=ItemsResponse)
    def indexes():
        return {"items": service.vectors.lists(service.operation())}

    @router.post("/indexes/build", status_code=202, response_model=OperationResponse)
    def build(options: BuildIndexRequest):
        return service.start("build-index", options.model_dump())

    @router.get("/indexes/{index_id}", response_model=IndexResponse)
    def inspect(index_id: str):
        if index_id not in service.vectors.catalog()["indexes"]:
            raise HTTPException(404, "Индекс не найден")
        return service.vectors.inspect(index_id, service.operation())

    @router.post("/indexes/{index_id}/rebuild", status_code=202, response_model=OperationResponse)
    def rebuild(index_id: str):
        record = service.vectors.catalog()["indexes"].get(index_id)
        if not record:
            raise HTTPException(404, "Индекс не найден")
        identity = record["identity"]
        return service.start(
            "build-index",
            {
                "model": identity["model"],
                "configuration_id": identity["preprocessing_configuration_id"],
                "rebuild": True,
                "heading_policy": identity["embedding_configuration"]["heading_policy"],
                "overflow_policy": identity["embedding_configuration"]["overflow_policy"],
            },
        )

    @router.get("/retrieval/configuration")
    def configuration():
        runs = service.store.processed_index()
        docs = list(service.store.manifest().documents.values())
        return {
            "default_configuration_id": runs["latest"],
            "preprocessing": [
                {
                    key: run[key]
                    for key in ("configuration_id", "configuration", "chunk_count", "source_commit")
                }
                for run in runs["runs"].values()
            ],
            "topics": sorted({d.topic for d in docs if d.status == "active"}),
            "documents": [
                {"document_id": d.document_id, "title": d.title} for d in docs if d.status == "active"
            ],
            "defaults": service.retrieval_config.model_dump(exclude={"models"}),
        }

    @router.get("/chunks/analysis")
    def analysis(configuration_id: str | None = None):
        return service.vectors.analysis(configuration_id)

    @router.post("/search", response_model=SearchResponse)
    def search(options: SearchRequest):
        return service.search(options)

    @router.get("/search/{request_id}", response_model=SearchResponse)
    def diagnostics(request_id: str):
        import time

        item = service.search_history.get(request_id)
        if not item or time.monotonic() - item[0] > 600:
            raise HTTPException(
                404, "Диагностика хранится только в памяти до 10 минут (последние 20 запросов)"
            )
        return item[1]

    app.include_router(router)
