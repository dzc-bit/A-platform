from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Message, SupportTicket, UserPreference
from ..schemas import AgentTrace, Citation
from .cache import RetrievalCache
from .knowledge import knowledge_version
from .runtime_settings import RuntimeSettings

if TYPE_CHECKING:
    from .agent import AgentResult


final_answer_cache = RetrievalCache(max_entries=256)
_ANSWER_CACHE_VERSION = "v1"


def build_answer_cache_key(
    db: Session,
    *,
    user_id: int,
    message: str,
    mode: str,
    conversation_id: int | None,
    preference: UserPreference,
    runtime_settings: RuntimeSettings,
) -> str:
    history: list[tuple[str, str]] = []
    if conversation_id is not None:
        rows = db.execute(
            select(Message.role, Message.content)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.id.desc())
            .limit(runtime_settings.conversation_memory_messages + 40)
        ).all()
        history = [(role, content) for role, content in reversed(rows)]

    ticket_distribution = [
        [status, priority, int(ticket_count)]
        for status, priority, ticket_count in db.execute(
            select(
                SupportTicket.status,
                SupportTicket.priority,
                func.count(SupportTicket.id),
            )
            .group_by(SupportTicket.status, SupportTicket.priority)
            .order_by(SupportTicket.status, SupportTicket.priority)
        ).all()
    ]
    basis = {
        "version": _ANSWER_CACHE_VERSION,
        "user_id": user_id,
        "message": " ".join(message.casefold().split()),
        "mode": mode,
        "history": history,
        "knowledge": knowledge_version(db),
        "tickets": ticket_distribution,
        "assistant_prompt": runtime_settings.assistant_prompt,
        "default_language": runtime_settings.default_language,
        "reply_strategy": runtime_settings.reply_strategy,
        "llm_model": runtime_settings.llm_model,
        "top_k": runtime_settings.retrieval_top_k,
        "memory_messages": runtime_settings.conversation_memory_messages,
        "preference": [
            preference.response_style,
            preference.preferred_language,
            preference.auto_play_voice,
        ],
    }
    digest = hashlib.sha256(
        json.dumps(basis, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"{settings.redis_key_prefix}:answer:{_ANSWER_CACHE_VERSION}:{digest}"


def serialize_agent_result(result: AgentResult) -> str:
    return json.dumps(
        {
            "answer": result.answer,
            "citations": [citation.model_dump() for citation in result.citations],
            "trace": [trace.model_dump() for trace in result.trace],
            "used_fallback": result.used_fallback,
            "category": result.category,
            "quality_score": result.quality_score,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def deserialize_agent_result(raw: str | None) -> AgentResult | None:
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict) or not isinstance(payload.get("answer"), str):
            return None
        from .agent import AgentResult

        return AgentResult(
            answer=payload["answer"],
            citations=[Citation.model_validate(item) for item in payload.get("citations", [])],
            trace=[AgentTrace.model_validate(item) for item in payload.get("trace", [])],
            used_fallback=bool(payload.get("used_fallback")),
            category=str(payload.get("category", "一般咨询")),
            quality_score=float(payload.get("quality_score") or 0.0),
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def should_cache_result(result: AgentResult) -> bool:
    # Dify media references may be signed and short-lived. Never replay them
    # from the answer cache after their validity window has elapsed.
    if result.category in {"语音生成", "图片生成"}:
        return False
    return not result.artifacts and (not settings.llm_api_key or not result.used_fallback)
