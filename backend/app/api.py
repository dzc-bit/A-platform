"""Aggregated API router: domain modules live in app/routers/."""

from fastapi import APIRouter

from .config import settings
from .routers.admin import router as admin_router
from .routers.auth import router as auth_router
from .routers.chat import router as chat_router
from .routers.chat import stream_chat
from .routers.dashboard import router as dashboard_router
from .routers.knowledge import router as knowledge_router
from .routers.media import router as media_router
from .routers.shared import (
    _normalize_dify_router_outputs,
    _token_secret_security,
    cancel_ticket_enrichment_tasks,
    workflow,
)
from .routers.support import router as support_router
from .routers.system import router as system_router
from .routers.users import router as users_router

router = APIRouter(prefix=settings.api_prefix)
router.include_router(system_router)
router.include_router(auth_router)
router.include_router(users_router)
router.include_router(chat_router)
router.include_router(knowledge_router)
router.include_router(support_router)
router.include_router(admin_router)
router.include_router(dashboard_router)
router.include_router(media_router)
