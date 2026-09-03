"""System health routes (mechanically split from app/api.py)."""

from . import shared
from .shared import *  # noqa: F401,F403

router = APIRouter()

@router.get("/health", tags=["system"])
def health_check() -> dict[str, object]:
    cache_status = retrieval_cache.status()
    answer_cache_status = final_answer_cache.status()
    return {
        "status": "ok",
        "service": shared.settings.app_name,
        "provider": "openai_compatible" if shared.settings.llm_api_key else "local_demo",
        "langgraph_available": LANGGRAPH_AVAILABLE,
        "dify_configured": bool(shared.settings.dify_api_url and shared.settings.dify_api_key),
        "security": {"token_secret": _token_secret_security(shared.settings.token_secret)},
        "cache": {
            "mode": cache_status.mode,
            "hits": cache_status.hits,
            "misses": cache_status.misses,
        },
        "answer_cache": {
            "mode": answer_cache_status.mode,
            "hits": answer_cache_status.hits,
            "misses": answer_cache_status.misses,
        },
    }
