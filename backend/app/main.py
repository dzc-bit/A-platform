from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api import cancel_ticket_enrichment_tasks, router
from .config import settings
from .database import SessionLocal, init_database
from .services.seed import seed_demo_data
from .services.knowledge import ensure_documents_indexed

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_database()
    with SessionLocal() as db:
        seed_demo_data(db)
        ensure_documents_indexed(db)
    logger.info("%s 已启动", settings.app_name)
    try:
        yield
    finally:
        await cancel_ticket_enrichment_tasks()


app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    description="Vue3 + FastAPI + RAG + 多智能体的离线优先商务服务平台",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": "请求参数校验失败", "errors": exc.errors()})


@app.get("/", tags=["system"])
def root() -> dict[str, str]:
    return {"service": settings.app_name, "docs": "/docs", "health": f"{settings.api_prefix}/health"}


app.include_router(router)
