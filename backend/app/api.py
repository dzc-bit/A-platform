from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import re
import secrets
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from uuid import uuid4
from xml.etree import ElementTree

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import Response, StreamingResponse
from pypdf import PdfReader
from pypdf.errors import PdfReadError
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from .config import settings
from .database import SessionLocal, get_db
from .dependencies import get_current_user, require_roles
from .models import (
    AdminAuditLog,
    AISetting,
    Conversation,
    KnowledgeDocument,
    Message,
    SupportNotification,
    SupportTicket,
    User,
)
from .schemas import (
    AdminAuditLogOut,
    AdminAuditLogPage,
    AgentTrace,
    Artifact,
    AuthResponse,
    ChatRequest,
    ChatResponse,
    Citation,
    ConversationAssignmentUpdate,
    ConversationAuditDetail,
    ConversationAuditSummary,
    ConversationFeedbackOut,
    ConversationFeedbackRequest,
    ConversationMessageCreate,
    ConversationOut,
    ConversationStatusUpdate,
    DashboardDetailScope,
    DashboardDetailsOut,
    DifyWorkflowRequest,
    DifyWorkflowResponse,
    DifyMediaResponse,
    DifyMediaProxyRequest,
    DifyTextToImageRequest,
    DifyTextToSpeechRequest,
    ExecutiveNotificationRequest,
    ImageAnalysisResponse,
    ExecutiveTakeoverRequest,
    KnowledgeCreate,
    KnowledgeOut,
    KnowledgeReindexOut,
    HandoffResponse,
    LangGraphCallbackRequest,
    LangGraphCallbackResponse,
    LoginRequest,
    MessageOut,
    RegisterRequest,
    SearchRequest,
    SearchResponse,
    SupportAssistantRequest,
    SupportAssistantResponse,
    SupportNotificationOut,
    SettingOut,
    SettingUpdate,
    TicketCreate,
    TicketOut,
    TicketUpdate,
    UserCreate,
    UserOut,
    UserPreferenceOut,
    UserPreferenceUpdate,
    UserResetPassword,
    UserRoleUpdate,
)
from .security import create_access_token, hash_password, verify_password
from .services.agent import (
    AgentResult,
    AgentStreamCompleted,
    AgentStreamReset,
    AgentStreamToken,
    AgentStreamTrace,
    BusinessAgentOrchestrator,
    LANGGRAPH_AVAILABLE,
)
from .services.answer_cache import (
    build_answer_cache_key,
    deserialize_agent_result,
    final_answer_cache,
    serialize_agent_result,
    should_cache_result,
)
from .services.cache import retrieval_cache
from .services.dify import DifyGateway, DifyMediaProxyError
from .services.events import ticket_event_broker
from .services.knowledge import index_document, remove_document, retrieve
from .services.preferences import get_user_preference, preference_instruction
from .services.runtime_settings import get_runtime_settings, validate_setting, SETTING_DEFAULTS, SETTING_DESCRIPTIONS
from .services.admin_audit import record_admin_action
from .services.vision import VisionService

router = APIRouter(prefix=settings.api_prefix)
orchestrator = BusinessAgentOrchestrator()
dify_gateway = DifyGateway()
vision_service = VisionService()

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_EXTRACTED_TEXT_CHARS = 100_000
MAX_DOCX_MEMBERS = 256
MAX_DOCX_UNCOMPRESSED_BYTES = 12 * 1024 * 1024
MAX_DOCX_COMPRESSION_RATIO = 100
MAX_PDF_PAGES = 200
MAX_CSV_ROWS = 20_000
MAX_CSV_COLUMNS = 256
SUPPORTED_DOCUMENT_SUFFIXES = {".txt", ".md", ".csv", ".pdf", ".docx"}
MAX_IMAGE_UPLOAD_BYTES = 5 * 1024 * 1024
TICKET_SUGGESTION_PENDING = "AI 建议正在生成，请稍候；客服可以先接管处理。"
_TICKET_TRANSITIONS: dict[str, frozenset[str]] = {
    "open": frozenset({"open", "in_progress", "resolved"}),
    "in_progress": frozenset({"in_progress", "open", "resolved"}),
    # Reopening a resolved ticket is explicit; a stale update cannot silently
    # move it back to the pending queue.
    "resolved": frozenset({"resolved", "open"}),
}
_HANDOFF_CATEGORIES = frozenset({"系统故障", "付款咨询", "合同咨询"})
_ticket_tasks: set[asyncio.Task[None]] = set()
DEMO_TOKEN_SECRETS = frozenset(
    {
        "change-this-before-production",
        "replace-with-a-long-random-secret-before-production",
        "replace-with-a-long-random-secret",
    }
)


def _handoff_available(result: AgentResult) -> bool:
    """Return whether the answer should offer the explicit human handoff."""
    return result.category in _HANDOFF_CATEGORIES or not result.citations or result.used_fallback


def _public_ticket_event(event: dict[str, object]) -> dict[str, object]:
    """Remove internal routing metadata before an SSE payload is serialized."""
    return {key: value for key, value in event.items() if key not in {"requester_id", "owner_id"}}


def _ticket_event(action: str, ticket: SupportTicket) -> dict[str, object]:
    return {
        "kind": "ticket",
        "action": action,
        "requester_id": ticket.requester_id,
        "ticket": TicketOut.model_validate(ticket).model_dump(mode="json"),
    }


def _conversation_event(action: str, conversation: Conversation, message: Message | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "kind": "conversation",
        "action": action,
        "owner_id": conversation.user_id,
        "conversation_id": conversation.id,
        "status": conversation.handoff_status,
        "assigned_agent_id": conversation.assigned_agent_id,
        "customer_id": conversation.user_id,
        "takeover_by_id": conversation.takeover_by_id,
        "takeover_notice": conversation.takeover_notice,
        "notice": conversation.takeover_notice,
        "control_mode": "executive_takeover" if conversation.takeover_by_id else "support_agent",
    }
    if message is not None:
        payload["message"] = MessageOut.model_validate(message).model_dump(mode="json")
    return payload


def _public_conversation_event(event: dict[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in event.items()
        if key not in {"owner_id", "requester_id", "assigned_agent_id"}
    }


def _public_support_conversation_event(event: dict[str, object]) -> dict[str, object]:
    """Keep routing fields for authenticated support SSE consumers."""
    return {key: value for key, value in event.items() if key != "owner_id"}


def _notification_event(
    action: str,
    notification: SupportNotification,
    *,
    conversation: Conversation | None = None,
) -> dict[str, object]:
    """Build a targeted management notice for the shared SSE broker."""
    payload: dict[str, object] = {
        "kind": "notification",
        "action": action,
        "recipient_id": notification.recipient_id,
        "sender_id": notification.sender_id,
        "agent_id": notification.recipient_id,
        "conversation_id": notification.conversation_id,
        "notice": notification.content,
        "message": notification.content,
        "notification": SupportNotificationOut.model_validate(notification).model_dump(mode="json"),
    }
    if conversation is not None:
        payload.update(
            {
                "status": conversation.handoff_status,
                "assigned_agent_id": conversation.assigned_agent_id,
                "takeover_by_id": conversation.takeover_by_id,
            }
        )
    return payload


_TICKET_PRIORITY_ORDER = {"urgent": 4, "high": 3, "normal": 2, "low": 1}


def _conversation_queue_payload(db: Session, conversation: Conversation) -> dict[str, object]:
    """Build one rich row for the support agent's multi-user queue.

    The original API returned a bare ``Conversation`` ORM row.  That made it
    impossible for a support UI to render a useful queue without issuing one
    request per conversation (and encouraged reusing the enterprise chat
    layout).  Keep the query deliberately straightforward for the demo-sized
    SQLite store: load the owner, messages and linked tickets, then expose a
    compact preview plus explicit counts.
    """
    owner = db.get(User, conversation.user_id)
    messages = list(
        db.scalars(
            select(Message)
            .where(Message.conversation_id == conversation.id)
            .order_by(Message.created_at.asc(), Message.id.asc())
        ).all()
    )
    recent = messages[-1] if messages else None

    # A message is unread for support until the most recent agent message.  AI
    # and pre-handoff user turns are intentionally excluded from the badge;
    # only customer turns waiting for a human response count as unread.
    latest_agent_index = max(
        (index for index, message in enumerate(messages) if message.role in {"agent", "support_agent"}),
        default=-1,
    )
    # System turns are emitted for handoff/read/close boundaries.  Treating
    # the latest one as a boundary avoids depending on localized message text
    # (and keeps unread calculations stable when the UI language changes).
    latest_system_index = max(
        (index for index, message in enumerate(messages) if message.role == "system"),
        default=-1,
    )
    unread_start = max(latest_agent_index, latest_system_index)
    unread_count = sum(message.role == "user" for message in messages[unread_start + 1 :])

    tickets = list(
        db.scalars(
            select(SupportTicket)
            .where(SupportTicket.conversation_id == conversation.id)
            .order_by(SupportTicket.updated_at.desc(), SupportTicket.id.desc())
        ).all()
    )
    # Prefer an unresolved ticket and then the highest priority when a
    # conversation has more than one linked ticket.
    tickets.sort(
        key=lambda ticket: (
            ticket.status == "resolved",
            -_TICKET_PRIORITY_ORDER.get(ticket.priority, 0),
            -(ticket.updated_at.timestamp() if ticket.updated_at else 0),
        )
    )
    related_ticket = tickets[0] if tickets else None
    related_ticket_payload = (
        TicketOut.model_validate(related_ticket).model_dump(mode="json")
        if related_ticket is not None
        else None
    )
    owner_payload = (
        {
            "id": owner.id,
            "email": owner.email,
            "display_name": owner.display_name,
            "role": owner.role,
        }
        if owner is not None
        else None
    )
    recent_payload = MessageOut.model_validate(recent).model_dump(mode="json") if recent else None
    assigned_agent = db.get(User, conversation.assigned_agent_id) if conversation.assigned_agent_id else None
    assigned_payload = (
        {
            "id": assigned_agent.id,
            "display_name": assigned_agent.display_name,
            "email": assigned_agent.email,
        }
        if assigned_agent is not None
        else None
    )
    takeover_by = db.get(User, conversation.takeover_by_id) if conversation.takeover_by_id else None
    takeover_payload = (
        {
            "id": takeover_by.id,
            "display_name": takeover_by.display_name,
            "email": takeover_by.email,
            "role": takeover_by.role,
        }
        if takeover_by is not None
        else None
    )
    priority = related_ticket.priority if related_ticket is not None else "normal"
    latest_notification = db.scalar(
        select(SupportNotification)
        .where(SupportNotification.conversation_id == conversation.id)
        .order_by(SupportNotification.created_at.desc(), SupportNotification.id.desc())
    )
    return {
        "id": conversation.id,
        "title": conversation.title,
        "mode": conversation.mode,
        "handoff_status": conversation.handoff_status,
        "status": conversation.handoff_status,
        "assigned_agent_id": conversation.assigned_agent_id,
        "assigned_agent": assigned_payload,
        "takeover_by_id": conversation.takeover_by_id,
        "takeover_by": takeover_payload,
        "takeover_notice": conversation.takeover_notice,
        "takeover_at": conversation.takeover_at,
        "control_mode": "executive_takeover" if conversation.takeover_by_id else "support_agent",
        "last_notification": (
            SupportNotificationOut.model_validate(latest_notification).model_dump(mode="json")
            if latest_notification is not None
            else None
        ),
        "updated_at": conversation.updated_at,
        "user_id": conversation.user_id,
        "customer_id": owner.id if owner else conversation.user_id,
        "customer_name": owner.display_name if owner else conversation.title,
        "customer_display_name": owner.display_name if owner else conversation.title,
        "customer_email": owner.email if owner else None,
        "user": owner_payload,
        "customer": owner_payload,
        "unread_count": unread_count,
        "priority": priority,
        "related_ticket_id": related_ticket.id if related_ticket else None,
        "ticket_id": related_ticket.id if related_ticket else None,
        "related_ticket": related_ticket_payload,
        "recent_message": recent_payload,
        "last_message": recent_payload,
        "feedback_rating": conversation.feedback_rating,
        "feedback_helpful": conversation.feedback_helpful,
        "feedback_comment": conversation.feedback_comment,
        "feedback_submitted_at": conversation.feedback_submitted_at,
    }


def _conversation_response(db: Session, conversation: Conversation) -> ConversationOut:
    return ConversationOut.model_validate(_conversation_queue_payload(db, conversation))


def _audit_json_array(value: str | None) -> list[object]:
    """Decode persisted trace/citation payloads without breaking old rows."""
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _audit_message_payload(message: Message) -> dict[str, object]:
    """Serialize one message with explicit actor and audit metadata."""
    payload = MessageOut.model_validate(message).model_dump(mode="json")
    payload.update(
        {
            "trace": _audit_json_array(message.trace_json),
            "citations": _audit_json_array(message.citations_json),
        }
    )
    return payload


def _conversation_audit_payload(
    db: Session,
    conversation: Conversation,
    *,
    include_messages: bool,
) -> dict[str, object]:
    """Build the shared admin/executive conversation audit contract."""
    owner = db.get(User, conversation.user_id)
    messages = list(
        db.scalars(
            select(Message)
            .where(Message.conversation_id == conversation.id)
            .order_by(Message.created_at.asc(), Message.id.asc())
        ).all()
    )
    serialized_messages = [_audit_message_payload(message) for message in messages]
    owner_payload = (
        {
            "id": owner.id,
            "email": owner.email,
            "display_name": owner.display_name,
            "role": owner.role,
        }
        if owner is not None
        else None
    )
    customer_name = owner.display_name if owner is not None else conversation.title
    customer_email = owner.email if owner is not None else None
    assigned_agent = db.get(User, conversation.assigned_agent_id) if conversation.assigned_agent_id else None
    assigned_payload = (
        {
            "id": assigned_agent.id,
            "email": assigned_agent.email,
            "display_name": assigned_agent.display_name,
            "role": assigned_agent.role,
        }
        if assigned_agent is not None
        else None
    )
    payload: dict[str, object] = {
        "id": conversation.id,
        "title": conversation.title,
        "mode": conversation.mode,
        "handoff_status": conversation.handoff_status,
        "status": conversation.handoff_status,
        "updated_at": conversation.updated_at,
        "user_id": conversation.user_id,
        "customer_name": customer_name,
        "customer_email": customer_email,
        "user": owner_payload,
        "customer": owner_payload,
        "assigned_agent_id": conversation.assigned_agent_id,
        "assigned_agent": assigned_payload,
        "takeover_by_id": conversation.takeover_by_id,
        "takeover_notice": conversation.takeover_notice,
        "takeover_at": conversation.takeover_at,
        "control_mode": "executive_takeover" if conversation.takeover_by_id else "support_agent",
        "message_count": len(serialized_messages),
        "recent_message": serialized_messages[-1] if serialized_messages else None,
        "feedback_rating": conversation.feedback_rating,
        "feedback_helpful": conversation.feedback_helpful,
        "feedback_comment": conversation.feedback_comment,
        "feedback_submitted_at": conversation.feedback_submitted_at,
    }
    if include_messages:
        payload["messages"] = serialized_messages
    return payload


def _conversation_audit_summary(
    db: Session,
    conversation: Conversation,
) -> ConversationAuditSummary:
    return ConversationAuditSummary.model_validate(
        _conversation_audit_payload(db, conversation, include_messages=False)
    )


def _conversation_message(db: Session, conversation: Conversation, role: str, content: str) -> Message:
    message = Message(conversation_id=conversation.id, role=role, content=content.strip())
    db.add(message)
    conversation.updated_at = datetime.now(timezone.utc)
    db.flush()
    return message


def _support_conversation(
    db: Session,
    conversation_id: int,
    user: User,
    *,
    allow_closed: bool = False,
) -> Conversation:
    conversation = db.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")
    if user.role == "admin":
        return conversation
    allowed_statuses = {"requested", "active"}
    if allow_closed:
        allowed_statuses.add("closed")
    if user.role != "support_agent" or conversation.handoff_status not in allowed_statuses:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="当前会话尚未转人工或无权访问")
    if conversation.assigned_agent_id not in {None, user.id}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="该会话已分配给其他客服")
    return conversation


def _executive_conversation(
    db: Session,
    conversation_id: int,
    user: User,
    *,
    allow_closed: bool = False,
) -> Conversation:
    """Resolve a conversation for management-only takeover operations."""
    if user.role not in {"executive", "admin"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="仅经营管理者或管理员可执行此操作")
    conversation = db.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")
    if not allow_closed and conversation.handoff_status == "closed":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="已结束的会话不能再次接管")
    if conversation.handoff_status not in {"ai", "requested", "active", "closed"}:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="当前会话尚未转人工")
    return conversation


def _ensure_support_control_allowed(conversation: Conversation, user: User) -> None:
    if user.role == "support_agent" and conversation.takeover_by_id is not None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="经营管理者已接管该会话，客服当前仅接收通知")


async def _enrich_ticket_suggestion(ticket_id: int, question: str) -> None:
    """Generate the expensive AI draft after the ticket response is sent."""
    try:
        with SessionLocal() as task_db:
            result = await orchestrator.run(task_db, question)
            ticket = task_db.get(SupportTicket, ticket_id)
            # Do not overwrite a human action that happened while the model
            # was running.  In particular, a resolved ticket never receives a
            # late, stale ``updated`` event from this task.
            if (
                ticket is None
                or ticket.status != "open"
                or ticket.final_reply is not None
                or ticket.suggested_reply != TICKET_SUGGESTION_PENDING
            ):
                return
            ticket.category = result.category
            ticket.suggested_reply = result.answer
            ticket.quality_score = 0.92 if result.citations else 0.74
            task_db.commit()
            task_db.refresh(ticket)
            event = _ticket_event("updated", ticket)
        if ticket_event_broker.has_subscribers():
            await ticket_event_broker.publish(event)
    except Exception:
        # A failed draft must remain actionable.  Never turn a successful 201
        # into a failed request merely because the optional AI enrichment died.
        try:
            with SessionLocal() as task_db:
                ticket = task_db.get(SupportTicket, ticket_id)
                if (
                    ticket is None
                    or ticket.status != "open"
                    or ticket.final_reply is not None
                    or ticket.suggested_reply != TICKET_SUGGESTION_PENDING
                ):
                    return
                ticket.suggested_reply = "AI 建议生成失败，请由客服人工处理。"
                task_db.commit()
                task_db.refresh(ticket)
                event = _ticket_event("updated", ticket)
            if ticket_event_broker.has_subscribers():
                await ticket_event_broker.publish(event)
        except Exception:
            # Background errors are intentionally isolated from the request
            # lifecycle; the ticket remains visible for manual handling.
            return


def _schedule_ticket_enrichment(ticket_id: int, question: str) -> None:
    """Detach enrichment from the request while retaining task references."""
    task = asyncio.create_task(_enrich_ticket_suggestion(ticket_id, question))
    _ticket_tasks.add(task)

    def finish(completed: asyncio.Task[None]) -> None:
        _ticket_tasks.discard(completed)
        # Calling result consumes exceptions so an enrichment failure cannot
        # become an unhandled-task warning in the server log.
        try:
            completed.result()
        except (asyncio.CancelledError, Exception):
            return

    task.add_done_callback(finish)


async def cancel_ticket_enrichment_tasks() -> None:
    """Cancel detached demo tasks during application shutdown/tests."""
    tasks = tuple(_ticket_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _ticket_tasks.clear()


def _token_secret_security(secret: str) -> dict[str, object]:
    using_demo_default = not secret or secret in DEMO_TOKEN_SECRETS
    return {
        "status": "warning" if using_demo_default else "configured",
        "using_demo_default": using_demo_default,
        "warning": (
            "TOKEN_SECRET 仍为演示默认值；对外部署前必须更换为强随机密钥。"
            if using_demo_default
            else None
        ),
    }


def _auth_response(user: User) -> AuthResponse:
    return AuthResponse(access_token=create_access_token(user.id, user.role), user=UserOut.model_validate(user))


def _image_media_type(payload: bytes) -> str | None:
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if payload.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(payload) >= 12 and payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return "image/webp"
    return None


def _owned_conversation(db: Session, conversation_id: int, user: User) -> Conversation:
    conversation = db.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")
    if conversation.user_id != user.id and user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权访问该会话")
    return conversation


def _begin_chat(db: Session, user: User, payload: ChatRequest) -> Conversation:
    if payload.conversation_id is None:
        conversation = Conversation(
            user_id=user.id,
            title=payload.message.strip()[:42],
            mode=payload.mode,
        )
        db.add(conversation)
        db.flush()
    else:
        conversation = _owned_conversation(db, payload.conversation_id, user)
        if conversation.handoff_status in {"requested", "active"}:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="该会话已转人工，请使用人工消息通道继续沟通",
            )

    db.add(Message(conversation_id=conversation.id, role="user", content=payload.message.strip()))
    db.flush()
    return conversation


def _persist_chat_result(db: Session, conversation: Conversation, result: AgentResult) -> ChatResponse:
    db.add(
        Message(
            conversation_id=conversation.id,
            role="assistant",
            content=result.answer,
            trace_json=json.dumps([trace.model_dump() for trace in result.trace], ensure_ascii=False),
            citations_json=json.dumps([citation.model_dump() for citation in result.citations], ensure_ascii=False),
            artifacts_json=json.dumps([artifact.model_dump() for artifact in result.artifacts], ensure_ascii=False),
        )
    )
    conversation.updated_at = datetime.now(timezone.utc)
    db.commit()
    return ChatResponse(
        conversation_id=conversation.id,
        answer=result.answer,
        citations=result.citations,
        trace=result.trace,
        used_fallback=result.used_fallback,
        artifacts=result.artifacts,
        handoff_available=_handoff_available(result),
        handoff_requested=conversation.handoff_status in {"requested", "active"},
    )


def _cache_hit_result(result: AgentResult) -> AgentResult:
    return AgentResult(
        answer=result.answer,
        citations=result.citations,
        trace=[
            *result.trace,
            AgentTrace(
                step="最终回答缓存",
                status="completed",
                detail="命中当前用户、上下文、知识版本与偏好对应的最终回答缓存",
            ),
        ],
        used_fallback=result.used_fallback,
        category=result.category,
        artifacts=result.artifacts,
    )


def _normalize_dify_router_outputs(outputs: dict[str, object]) -> dict[str, object]:
    """Map the active Dify end-node prefix to the canonical output contract."""
    names = (
        "answer",
        "citations",
        "trace",
        "artifacts",
        "need_clarification",
        "category",
        "used_fallback",
    )
    prefixes = ("a_", "b_", "c_")
    active_prefix = next(
        (
            prefix
            for prefix in prefixes
            if isinstance(outputs.get(f"{prefix}answer"), str)
            and str(outputs[f"{prefix}answer"]).strip()
        ),
        None,
    )
    if active_prefix is None:
        active_prefix = next(
            (prefix for prefix in prefixes if any(f"{prefix}{name}" in outputs for name in names)),
            None,
        )
    if active_prefix is None:
        return outputs

    normalized = dict(outputs)
    for name in names:
        prefixed_name = f"{active_prefix}{name}"
        if name not in normalized and prefixed_name in outputs:
            normalized[name] = outputs[prefixed_name]
    return normalized


async def _run_chat_via_router_or_local(
    db: Session,
    user: User,
    payload: ChatRequest,
    conversation: Conversation,
    preference,
    runtime_settings,
) -> AgentResult:
    """Try Dify router workflow first; fall back to local LangGraph orchestrator."""
    if settings.dify_router_api_key and settings.dify_api_url:
        rows = db.execute(
            select(Message.role, Message.content)
            .where(
                Message.conversation_id == conversation.id,
                Message.role.in_(("user", "assistant")),
            )
            .order_by(Message.id.desc())
            .limit(20)
        ).all()
        context = [
            {"role": role, "content": content.strip()[:8000]}
            for role, content in reversed(rows)
            if content.strip()
        ]
        while context and len(json.dumps(context, ensure_ascii=False, separators=(",", ":"))) > 19_000:
            context.pop(0)
        router_result = await dify_gateway.run_router_workflow(
            payload.message.strip(),
            str(user.id),
            context=context,
            conversation_id=conversation.id,
            request_id=str(uuid4()),
        )
        if not router_result.degraded and router_result.outputs:
            outputs = _normalize_dify_router_outputs(router_result.outputs)
            answer = outputs.get("answer") or outputs.get("text") or ""
            if isinstance(answer, str) and answer.strip():
                def output_objects(name: str) -> list[dict[str, object]]:
                    value = outputs.get(name, [])
                    if isinstance(value, str):
                        try:
                            value = json.loads(value)
                        except json.JSONDecodeError:
                            return []
                    if not isinstance(value, list):
                        return []
                    return [item for item in value if isinstance(item, dict)]

                citations = []
                for index, item in enumerate(output_objects("citations")):
                    try:
                        metadata = item.get("metadata")
                        metadata = metadata if isinstance(metadata, dict) else {}
                        document_id = (
                            item.get("document_id")
                            or metadata.get("document_id")
                            or metadata.get("dataset_id")
                            or f"dify-{index}"
                        )
                        citations.append(Citation(
                            document_id=document_id,
                            title=str(
                                item.get("title")
                                or metadata.get("document_name")
                                or metadata.get("name")
                                or "Dify 知识库"
                            ),
                            excerpt=str(item.get("excerpt") or item.get("content") or item.get("text") or ""),
                            score=float(item.get("score", metadata.get("score", 0))),
                        ))
                    except (TypeError, ValueError):
                        continue
                trace = [AgentTrace(
                    step="Dify 路由工作流",
                    status="completed",
                    detail=router_result.detail,
                )]
                for item in output_objects("trace"):
                    item_status = item.get("status", "completed")
                    if item_status not in {"completed", "skipped", "fallback"}:
                        item_status = "completed"
                    trace.append(AgentTrace(
                        step=str(item.get("step", "")),
                        status=item_status,
                        detail=str(item.get("detail", "")),
                    ))
                artifacts = []
                for item in output_objects("artifacts"):
                    try:
                        artifacts.append(Artifact.model_validate(item))
                    except (TypeError, ValueError):
                        continue
                return AgentResult(
                    answer=answer.strip(),
                    citations=citations,
                    trace=trace,
                    used_fallback=(
                        str(outputs.get("used_fallback", "false")).strip().casefold()
                        in {"1", "true", "yes"}
                    ),
                    category=str(outputs.get("category", "")),
                    artifacts=artifacts,
                )
    # Fallback: local LangGraph orchestrator.
    return await orchestrator.run(
        db,
        payload.message.strip(),
        conversation_id=conversation.id,
        preference_instruction=preference_instruction(preference),
    )


async def _run_chat(db: Session, user: User, payload: ChatRequest) -> ChatResponse:
    preference = get_user_preference(db, user.id)
    if payload.conversation_id is not None:
        _owned_conversation(db, payload.conversation_id, user)
    runtime_settings = get_runtime_settings(db)
    cache_key = build_answer_cache_key(
        db,
        user_id=user.id,
        message=payload.message,
        mode=payload.mode,
        conversation_id=payload.conversation_id,
        preference=preference,
        runtime_settings=runtime_settings,
    )
    cached = (
        deserialize_agent_result(final_answer_cache.get(cache_key))
        if runtime_settings.answer_cache_ttl_seconds
        else None
    )
    conversation = _begin_chat(db, user, payload)
    if cached is None and settings.dify_router_api_key and settings.dify_api_url:
        # The Dify workflow calls this FastAPI process back on another request.
        # Do not keep a SQLite write transaction open across that network hop.
        db.commit()
        db.refresh(conversation)
    if cached is not None:
        result = _cache_hit_result(cached)
    else:
        result = await _run_chat_via_router_or_local(
            db, user, payload, conversation, preference, runtime_settings
        )
    response = _persist_chat_result(db, conversation, result)
    assistant_message = db.scalar(
        select(Message)
        .where(Message.conversation_id == conversation.id, Message.role == "assistant")
        .order_by(Message.id.desc())
    )
    if assistant_message is not None:
        await ticket_event_broker.publish(_conversation_event("message", conversation, assistant_message))
    if cached is None and runtime_settings.answer_cache_ttl_seconds and should_cache_result(result):
        final_answer_cache.set(
            cache_key,
            serialize_agent_result(result),
            runtime_settings.answer_cache_ttl_seconds,
        )
    return response


@router.get("/health", tags=["system"])
def health_check() -> dict[str, object]:
    cache_status = retrieval_cache.status()
    answer_cache_status = final_answer_cache.status()
    return {
        "status": "ok",
        "service": settings.app_name,
        "provider": "openai_compatible" if settings.llm_api_key else "local_demo",
        "langgraph_available": LANGGRAPH_AVAILABLE,
        "dify_configured": bool(settings.dify_api_url and settings.dify_api_key),
        "security": {"token_secret": _token_secret_security(settings.token_secret)},
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


@router.post("/auth/register", response_model=AuthResponse, status_code=status.HTTP_201_CREATED, tags=["auth"])
def register(payload: RegisterRequest, db: Session = Depends(get_db)) -> AuthResponse:
    email = payload.email.strip().lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该邮箱已注册")
    user = User(
        email=email,
        password_hash=hash_password(payload.password),
        display_name=payload.display_name.strip(),
        role="enterprise_user",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return _auth_response(user)


@router.post("/auth/login", response_model=AuthResponse, tags=["auth"])
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> AuthResponse:
    user = db.scalar(select(User).where(User.email == payload.email.strip().lower()))
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="邮箱或密码错误")
    if user.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="该账户已注销")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="该账户已停用")
    return _auth_response(user)


@router.get("/auth/me", response_model=UserOut, tags=["auth"])
def current_profile(current_user: User = Depends(get_current_user)) -> User:
    return current_user


@router.get("/users/me/preferences", response_model=UserPreferenceOut, tags=["users"])
def get_current_preferences(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    preference = get_user_preference(db, current_user.id)
    db.commit()
    db.refresh(preference)
    return preference


@router.put("/users/me/preferences", response_model=UserPreferenceOut, tags=["users"])
def update_current_preferences(
    payload: UserPreferenceUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    preference = get_user_preference(db, current_user.id)
    preference.response_style = payload.response_style
    preference.preferred_language = payload.preferred_language
    preference.auto_play_voice = payload.auto_play_voice
    db.commit()
    db.refresh(preference)
    return preference


@router.get("/assistant/conversations", response_model=list[ConversationOut], tags=["assistant"])
def list_conversations(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[Conversation]:
    statement = select(Conversation).where(Conversation.user_id == current_user.id).order_by(Conversation.updated_at.desc())
    return list(db.scalars(statement).all())


@router.get("/assistant/conversations/{conversation_id}/messages", response_model=list[MessageOut], tags=["assistant"])
def list_messages(
    conversation_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[Message]:
    _owned_conversation(db, conversation_id, current_user)
    statement = select(Message).where(Message.conversation_id == conversation_id).order_by(Message.created_at.asc())
    return list(db.scalars(statement).all())


@router.post(
    "/assistant/conversations/{conversation_id}/feedback",
    response_model=ConversationFeedbackOut,
    tags=["assistant"],
)
def submit_conversation_feedback(
    conversation_id: int,
    payload: ConversationFeedbackRequest,
    current_user: User = Depends(require_roles("enterprise_user")),
    db: Session = Depends(get_db),
) -> ConversationFeedbackOut:
    """Record one explicit end-of-conversation rating from its owner."""
    conversation = _owned_conversation(db, conversation_id, current_user)
    if conversation.feedback_submitted_at is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该会话已经提交过评价")
    has_answer = db.scalar(
        select(Message.id).where(
            Message.conversation_id == conversation.id,
            Message.role.in_(("assistant", "ai")),
        )
    )
    if has_answer is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="会话尚未生成 AI 回复，暂不能评价")
    submitted_at = datetime.now(timezone.utc)
    conversation.feedback_rating = payload.rating
    conversation.feedback_helpful = payload.helpful
    conversation.feedback_comment = payload.comment.strip() if payload.comment else None
    conversation.feedback_submitted_at = submitted_at
    conversation.updated_at = submitted_at
    db.commit()
    return ConversationFeedbackOut(
        rating=conversation.feedback_rating,
        helpful=conversation.feedback_helpful,
        comment=conversation.feedback_comment,
        submitted_at=submitted_at,
    )


@router.delete(
    "/assistant/conversations/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["assistant"],
)
def delete_conversation(
    conversation_id: int,
    current_user: User = Depends(require_roles("enterprise_user")),
    db: Session = Depends(get_db),
) -> None:
    """Permanently remove one enterprise user's conversation and its messages.

    Conversation messages do not use a database-level cascade because the demo
    schema must also work against existing SQLite volumes.  Delete dependent
    rows explicitly, and detach any optional ticket reference before removing
    the parent conversation so the operation remains valid when foreign keys
    are enforced.
    """
    conversation = _owned_conversation(db, conversation_id, current_user)
    db.execute(delete(Message).where(Message.conversation_id == conversation.id))
    db.execute(
        update(SupportTicket)
        .where(SupportTicket.conversation_id == conversation.id)
        .values(conversation_id=None)
    )
    db.delete(conversation)
    db.commit()


@router.post(
    "/assistant/conversations/{conversation_id}/handoff",
    response_model=HandoffResponse,
    tags=["assistant"],
)
async def request_human_handoff(
    conversation_id: int,
    current_user: User = Depends(require_roles("enterprise_user")),
    db: Session = Depends(get_db),
) -> HandoffResponse:
    conversation = _owned_conversation(db, conversation_id, current_user)
    if conversation.handoff_status == "active":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该会话已由客服接管")
    if conversation.handoff_status == "requested":
        existing = db.scalar(
            select(Message)
            .where(
                Message.conversation_id == conversation.id,
                Message.role == "system",
            )
            .order_by(Message.id.desc())
        )
        if existing is not None:
            return HandoffResponse(
                conversation_id=conversation.id,
                status="requested",
                message=MessageOut.model_validate(existing),
            )
    conversation.handoff_status = "requested"
    message = _conversation_message(db, conversation, "system", "已转人工，客服人员会在此会话中回复。")
    db.commit()
    db.refresh(conversation)
    db.refresh(message)
    await ticket_event_broker.publish(_conversation_event("handoff", conversation, message))
    return HandoffResponse(
        conversation_id=conversation.id,
        status="requested",
        message=MessageOut.model_validate(message),
    )


@router.post(
    "/assistant/conversations/{conversation_id}/messages",
    response_model=MessageOut,
    tags=["assistant"],
)
async def send_human_message(
    conversation_id: int,
    payload: ConversationMessageCreate,
    current_user: User = Depends(require_roles("enterprise_user")),
    db: Session = Depends(get_db),
) -> MessageOut:
    conversation = _owned_conversation(db, conversation_id, current_user)
    if conversation.handoff_status not in {"requested", "active"}:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="请先请求转人工")
    message = _conversation_message(db, conversation, "user", payload.content)
    db.commit()
    db.refresh(conversation)
    db.refresh(message)
    await ticket_event_broker.publish(_conversation_event("message", conversation, message))
    return MessageOut.model_validate(message)


@router.get("/assistant/conversations/{conversation_id}/events", tags=["assistant"])
async def stream_conversation_events(
    conversation_id: int,
    request: Request,
    current_user: User = Depends(require_roles("enterprise_user")),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    conversation = _owned_conversation(db, conversation_id, current_user)

    async def event_stream():
        def owned(event: dict[str, object]) -> bool:
            return event.get("kind") == "conversation" and event.get("owner_id") == conversation.user_id and event.get("conversation_id") == conversation.id

        async with ticket_event_broker.subscribe(owned) as queue:
            yield "event: ready\ndata: {}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    if await request.is_disconnected():
                        return
                    yield ": keep-alive\n\n"
                    continue
                yield f"event: conversation\ndata: {json.dumps(_public_conversation_event(event), ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/assistant/chat", response_model=ChatResponse, tags=["assistant"])
async def chat(
    payload: ChatRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ChatResponse:
    return await _run_chat(db, current_user, payload)


@router.post(
    "/support/assistant",
    response_model=SupportAssistantResponse,
    tags=["support"],
)
@router.post(
    "/support/assistant/chat",
    response_model=SupportAssistantResponse,
    include_in_schema=False,
    tags=["support"],
)
async def support_assistant(
    payload: SupportAssistantRequest,
    current_user: User = Depends(require_roles("support_agent", "admin")),
    db: Session = Depends(get_db),
) -> SupportAssistantResponse:
    """Return a private draft for a support agent; it never sends to a user."""
    if payload.conversation_id is not None:
        _support_conversation(db, payload.conversation_id, current_user, allow_closed=True)
    result = await orchestrator.run_support_assistant(
        db,
        payload.query.strip(),
        conversation_id=payload.conversation_id,
        use_knowledge=payload.use_knowledge,
    )
    return SupportAssistantResponse(
        answer=result.answer,
        citations=result.citations,
        trace=result.trace,
        used_fallback=result.used_fallback,
        model_mode="support_hybrid" if payload.use_knowledge else "support_general",
        model=get_runtime_settings(db).support_assistant_model,
        knowledge_used=bool(result.citations),
        category=result.category,
    )


@router.post("/assistant/chat/stream", tags=["assistant"])
async def stream_chat(
    payload: ChatRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    async def event_stream():
        committed = False
        dispatch_trace = AgentTrace(
            step="请求调度",
            status="completed",
            detail="已接收请求，正在执行分类、检索与回复 Agent",
        )
        try:
            yield f"event: trace\ndata: {json.dumps(dispatch_trace.model_dump(), ensure_ascii=False)}\n\n"
            if settings.dify_router_api_key and settings.dify_api_url:
                result = await _run_chat(db, current_user, payload)
                committed = True
                for trace in result.trace:
                    yield f"event: trace\ndata: {json.dumps(trace.model_dump(), ensure_ascii=False)}\n\n"
                yield (
                    "event: reset\ndata: "
                    f"{json.dumps({'text': result.answer}, ensure_ascii=False)}\n\n"
                )
                yield f"event: done\ndata: {result.model_dump_json()}\n\n"
                return
            preference = get_user_preference(db, current_user.id)
            if payload.conversation_id is not None:
                _owned_conversation(db, payload.conversation_id, current_user)
            runtime_settings = get_runtime_settings(db)
            cache_key = build_answer_cache_key(
                db,
                user_id=current_user.id,
                message=payload.message,
                mode=payload.mode,
                conversation_id=payload.conversation_id,
                preference=preference,
                runtime_settings=runtime_settings,
            )
            cached = (
                deserialize_agent_result(final_answer_cache.get(cache_key))
                if runtime_settings.answer_cache_ttl_seconds
                else None
            )
            conversation = _begin_chat(db, current_user, payload)
            if cached is not None:
                cached = _cache_hit_result(cached)
                for trace in cached.trace:
                    yield f"event: trace\ndata: {json.dumps(trace.model_dump(), ensure_ascii=False)}\n\n"
                yield (
                    "event: token\ndata: "
                    f"{json.dumps({'text': cached.answer, 'origin': 'cache'}, ensure_ascii=False)}\n\n"
                )
                result = _persist_chat_result(db, conversation, cached)
                committed = True
                assistant_message = db.scalar(
                    select(Message)
                    .where(Message.conversation_id == conversation.id, Message.role == "assistant")
                    .order_by(Message.id.desc())
                )
                if assistant_message is not None:
                    await ticket_event_broker.publish(_conversation_event("message", conversation, assistant_message))
                yield f"event: done\ndata: {result.model_dump_json()}\n\n"
                return
            async for event in orchestrator.stream(
                db,
                payload.message.strip(),
                conversation_id=conversation.id,
                preference_instruction=preference_instruction(preference),
            ):
                if isinstance(event, AgentStreamTrace):
                    yield f"event: trace\ndata: {json.dumps(event.trace.model_dump(), ensure_ascii=False)}\n\n"
                elif isinstance(event, AgentStreamToken):
                    yield (
                        "event: token\ndata: "
                        f"{json.dumps({'text': event.text, 'origin': event.origin}, ensure_ascii=False)}\n\n"
                    )
                elif isinstance(event, AgentStreamReset):
                    yield (
                        "event: reset\ndata: "
                        f"{json.dumps({'text': event.text}, ensure_ascii=False)}\n\n"
                    )
                elif isinstance(event, AgentStreamCompleted):
                    result = _persist_chat_result(db, conversation, event.result)
                    committed = True
                    assistant_message = db.scalar(
                        select(Message)
                        .where(Message.conversation_id == conversation.id, Message.role == "assistant")
                        .order_by(Message.id.desc())
                    )
                    if assistant_message is not None:
                        await ticket_event_broker.publish(_conversation_event("message", conversation, assistant_message))
                    if runtime_settings.answer_cache_ttl_seconds and should_cache_result(event.result):
                        final_answer_cache.set(
                            cache_key,
                            serialize_agent_result(event.result),
                            runtime_settings.answer_cache_ttl_seconds,
                        )
                    yield f"event: done\ndata: {result.model_dump_json()}\n\n"
                    return
            raise RuntimeError("Agent stream ended without a completion event")
        finally:
            if not committed:
                db.rollback()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/assistant/image-analysis", response_model=ImageAnalysisResponse, tags=["assistant"])
async def analyze_image(
    file: UploadFile = File(...),
    prompt: str = Form(default=""),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ImageAnalysisResponse:
    """Analyze one transient image without saving its bytes to the application database."""
    del current_user
    image_bytes = await file.read(MAX_IMAGE_UPLOAD_BYTES + 1)
    if not image_bytes:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="图片文件为空")
    if len(image_bytes) > MAX_IMAGE_UPLOAD_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="图片不能超过 5MB")
    media_type = _image_media_type(image_bytes)
    if media_type is None:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="仅支持 PNG、JPEG 和 WebP 图片",
        )
    normalized_prompt = prompt.strip()
    if len(normalized_prompt) > 1_000:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="图片问题不能超过 1000 个字符")
    runtime_settings = get_runtime_settings(db)
    result = await vision_service.analyze(
        image_bytes,
        media_type,
        normalized_prompt,
        model=runtime_settings.vision_model,
    )
    return ImageAnalysisResponse(
        answer=result.answer,
        used_fallback=result.used_fallback,
        detail=result.detail,
    )


@router.get("/knowledge/documents", response_model=list[KnowledgeOut], tags=["knowledge"])
def list_documents(
    current_user: User = Depends(require_roles("admin", "support_agent")), db: Session = Depends(get_db)
) -> list[KnowledgeDocument]:
    del current_user
    return list(db.scalars(select(KnowledgeDocument).order_by(KnowledgeDocument.updated_at.desc())).all())


@router.post("/knowledge/documents", response_model=KnowledgeOut, status_code=status.HTTP_201_CREATED, tags=["knowledge"])
def create_document(
    payload: KnowledgeCreate,
    current_user: User = Depends(require_roles("admin", "support_agent")),
    db: Session = Depends(get_db),
) -> KnowledgeDocument:
    del current_user
    document = KnowledgeDocument(title=payload.title.strip(), source=payload.source.strip(), content=payload.content.strip())
    db.add(document)
    db.flush()
    index_document(db, document)
    db.commit()
    db.refresh(document)
    return document


@router.put("/knowledge/documents/{document_id}", response_model=KnowledgeOut, tags=["knowledge"])
def update_document(
    document_id: int,
    payload: KnowledgeCreate,
    current_user: User = Depends(require_roles("admin", "support_agent")),
    db: Session = Depends(get_db),
) -> KnowledgeDocument:
    del current_user
    document = db.get(KnowledgeDocument, document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="知识文档不存在")
    document.title = payload.title.strip()
    document.source = payload.source.strip()
    document.content = payload.content.strip()
    document.status = "indexing"
    db.flush()
    index_document(db, document)
    document.status = "ready"
    db.commit()
    db.refresh(document)
    return document


@router.delete(
    "/knowledge/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["knowledge"],
)
def delete_document(
    document_id: int,
    current_user: User = Depends(require_roles("admin", "support_agent")),
    db: Session = Depends(get_db),
) -> None:
    del current_user
    document = db.get(KnowledgeDocument, document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="知识文档不存在")
    remove_document(db, document)
    db.commit()


@router.post(
    "/knowledge/documents/{document_id}/reindex",
    response_model=KnowledgeReindexOut,
    tags=["knowledge"],
)
def reindex_document(
    document_id: int,
    current_user: User = Depends(require_roles("admin", "support_agent")),
    db: Session = Depends(get_db),
) -> KnowledgeReindexOut:
    del current_user
    document = db.get(KnowledgeDocument, document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="知识文档不存在")
    document.status = "indexing"
    db.flush()
    indexed_chunks = index_document(db, document)
    document.status = "ready"
    db.commit()
    db.refresh(document)
    return KnowledgeReindexOut(
        document=KnowledgeOut.model_validate(document),
        status=document.status,
        indexed_chunks=indexed_chunks,
    )


def _safe_uploaded_filename(filename: str | None) -> str:
    raw_name = (filename or "upload.txt").replace("\\", "/").rsplit("/", maxsplit=1)[-1]
    cleaned = re.sub(r"[\x00-\x1f\x7f]", "", raw_name).strip()
    return cleaned[:255] or "upload.txt"


def _decode_text_payload(payload: bytes) -> str:
    if b"\x00" in payload:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="文本文件不能包含空字节")
    for encoding in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail="文本或 CSV 文件必须使用 UTF-8 或 GB18030 编码",
    )


def _normalize_extracted_text(text: str) -> str:
    # Strip control characters before persisting text that may later be rendered in an admin view.
    normalized = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", text)
    normalized = re.sub(r"[\t\r\n ]+", " ", normalized).strip()
    if len(normalized) > MAX_EXTRACTED_TEXT_CHARS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"解析后的文档不能超过 {MAX_EXTRACTED_TEXT_CHARS} 个字符",
        )
    return normalized


def _extract_csv_text(payload: bytes) -> str:
    source = _decode_text_payload(payload)
    try:
        reader = csv.reader(io.StringIO(source), strict=True)
        rows: list[str] = []
        for row_index, row in enumerate(reader, start=1):
            if row_index > MAX_CSV_ROWS:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"CSV 不能超过 {MAX_CSV_ROWS} 行",
                )
            if len(row) > MAX_CSV_COLUMNS:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"CSV 每行不能超过 {MAX_CSV_COLUMNS} 列",
                )
            rows.append(" | ".join(cell.strip() for cell in row))
    except csv.Error as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="无法解析 CSV 文件") from error
    return "\n".join(rows)


def _validate_docx_archive(document_zip: zipfile.ZipFile) -> zipfile.ZipInfo:
    infos = document_zip.infolist()
    if len(infos) > MAX_DOCX_MEMBERS:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="DOCX 包含过多文件")

    total_uncompressed = 0
    document_xml_infos: list[zipfile.ZipInfo] = []
    for info in infos:
        archive_path = PurePosixPath(info.filename)
        if info.flag_bits & 0x1:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="不支持加密的 DOCX 文件")
        if "\\" in info.filename or archive_path.is_absolute() or ".." in archive_path.parts:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="DOCX 文件结构无效")
        if info.file_size < 0 or info.file_size > MAX_DOCX_UNCOMPRESSED_BYTES:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="DOCX 解压后的内容过大")
        if info.file_size and (not info.compress_size or info.file_size / info.compress_size > MAX_DOCX_COMPRESSION_RATIO):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="DOCX 压缩比例异常")
        total_uncompressed += info.file_size
        if total_uncompressed > MAX_DOCX_UNCOMPRESSED_BYTES:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="DOCX 解压后的内容过大")
        if info.filename == "word/document.xml":
            document_xml_infos.append(info)

    if len(document_xml_infos) != 1:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="DOCX 缺少正文内容")
    return document_xml_infos[0]


def _extract_docx_text(payload: bytes) -> str:
    if not zipfile.is_zipfile(io.BytesIO(payload)):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="无法解析 DOCX 文件")
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as document_zip:
            document_xml = document_zip.read(_validate_docx_archive(document_zip))
        if b"<!DOCTYPE" in document_xml.upper() or b"<!ENTITY" in document_xml.upper():
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="DOCX XML 不允许包含实体声明")
        root = ElementTree.fromstring(document_xml)
    except (KeyError, OSError, zipfile.BadZipFile, ElementTree.ParseError) as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="无法解析 DOCX 文件") from error
    text_nodes = [node.text or "" for node in root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t")]
    return " ".join(text_nodes)


def _extract_pdf_text(payload: bytes) -> str:
    if not payload.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"%PDF-"):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="PDF 文件头无效")
    try:
        reader = PdfReader(io.BytesIO(payload), strict=True)
        if reader.is_encrypted:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="不支持加密的 PDF 文件")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"PDF 不能超过 {MAX_PDF_PAGES} 页",
            )
        text_parts: list[str] = []
        text_size = 0
        for page in reader.pages:
            page_text = page.extract_text() or ""
            text_size += len(page_text)
            if text_size > MAX_EXTRACTED_TEXT_CHARS:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"解析后的文档不能超过 {MAX_EXTRACTED_TEXT_CHARS} 个字符",
                )
            text_parts.append(page_text)
        return "\n".join(text_parts)
    except HTTPException:
        raise
    except (PdfReadError, KeyError, OSError, ValueError) as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="无法解析 PDF 文件") from error


def _extract_document_text(filename: str, payload: bytes) -> str:
    suffix = PurePosixPath(filename).suffix.lower()
    if suffix == ".doc":
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="不支持旧版 .doc 文件，请转换为 DOCX、PDF 或 CSV 后重试",
        )
    if suffix not in SUPPORTED_DOCUMENT_SUFFIXES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="仅支持 TXT、Markdown、CSV、PDF 和 DOCX 文件",
        )
    if suffix == ".docx":
        text = _extract_docx_text(payload)
    elif suffix == ".pdf":
        text = _extract_pdf_text(payload)
    elif suffix == ".csv":
        text = _extract_csv_text(payload)
    else:
        text = _decode_text_payload(payload)
    return _normalize_extracted_text(text)


@router.post("/knowledge/upload", response_model=KnowledgeOut, status_code=status.HTTP_201_CREATED, tags=["knowledge"])
@router.post(
    "/admin/knowledge/upload",
    response_model=KnowledgeOut,
    status_code=status.HTTP_201_CREATED,
    include_in_schema=False,
    tags=["knowledge"],
)
async def upload_document(
    file: UploadFile = File(...),
    title: str | None = Form(default=None),
    current_user: User = Depends(require_roles("admin", "support_agent")),
    db: Session = Depends(get_db),
) -> KnowledgeDocument:
    del current_user
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if not content:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="上传文件为空")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="单个文档不能超过 5MB")
    filename = _safe_uploaded_filename(file.filename)
    text = _extract_document_text(filename, content)
    if len(text) < 20:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="文档有效文本不足 20 个字符")
    document_title = (title or "").strip()[:255] or filename
    document = KnowledgeDocument(title=document_title, source=f"上传文件：{filename}"[:255], content=text)
    db.add(document)
    db.flush()
    index_document(db, document)
    db.commit()
    db.refresh(document)
    return document


@router.post("/knowledge/search", response_model=SearchResponse, tags=["knowledge"])
def search_knowledge(
    payload: SearchRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SearchResponse:
    del current_user
    hits = retrieve(db, payload.query, top_k=payload.top_k)
    return SearchResponse(
        results=[
            {"document_id": hit.document_id, "title": hit.title, "excerpt": hit.excerpt, "score": hit.score}
            for hit in hits
        ]
    )


@router.get("/support/tickets", response_model=list[TicketOut], tags=["support"])
def list_tickets(
    status_filter: str | None = Query(default=None, alias="status"),
    current_user: User = Depends(require_roles("support_agent", "admin")),
    db: Session = Depends(get_db),
) -> list[SupportTicket]:
    del current_user
    if status_filter is not None and status_filter not in {"all", "pending", "open", "in_progress", "resolved"}:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="不支持的工单状态筛选")
    statement = select(SupportTicket)
    if status_filter in {"open", "in_progress"}:
        statement = statement.where(SupportTicket.status == status_filter)
    elif status_filter == "pending":
        statement = statement.where(SupportTicket.status != "resolved")
    elif status_filter == "resolved":
        statement = statement.where(SupportTicket.status == "resolved")
    return list(db.scalars(statement.order_by(SupportTicket.updated_at.desc())).all())


@router.get("/support/tickets/mine", response_model=list[TicketOut], tags=["support"])
def list_my_tickets(
    status_filter: str | None = Query(default=None, alias="status"),
    current_user: User = Depends(require_roles("enterprise_user")),
    db: Session = Depends(get_db),
) -> list[SupportTicket]:
    if status_filter is not None and status_filter not in {"all", "pending", "open", "in_progress", "resolved"}:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="不支持的工单状态筛选")
    statement = select(SupportTicket).where(SupportTicket.requester_id == current_user.id)
    if status_filter in {"open", "in_progress"}:
        statement = statement.where(SupportTicket.status == status_filter)
    elif status_filter == "pending":
        statement = statement.where(SupportTicket.status != "resolved")
    elif status_filter == "resolved":
        statement = statement.where(SupportTicket.status == "resolved")
    return list(db.scalars(statement.order_by(SupportTicket.updated_at.desc())).all())


@router.post("/support/tickets", response_model=TicketOut, status_code=status.HTTP_201_CREATED, tags=["support"])
async def create_ticket(
    payload: TicketCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SupportTicket:
    linked_conversation: Conversation | None = None
    if payload.conversation_id is not None:
        # A customer may link only their own conversation.  Staff can create
        # an operational ticket from any handoff they are allowed to inspect.
        if current_user.role == "enterprise_user":
            linked_conversation = _owned_conversation(db, payload.conversation_id, current_user)
        elif current_user.role in {"support_agent", "admin"}:
            linked_conversation = _support_conversation(
                db,
                payload.conversation_id,
                current_user,
                allow_closed=True,
            )
        else:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="当前角色不能关联会话工单")
    try:
        category = orchestrator.classify(payload.question.strip())
    except (TypeError, ValueError, RuntimeError):
        category = "一般咨询"
    # A local/offline demo must remain deterministic and must not leave a
    # detached SQLite task holding a connection after the request.  When a
    # real model key is configured, the expensive draft is generated after
    # the response by ``_schedule_ticket_enrichment``.
    initial_reply = (
        TICKET_SUGGESTION_PENDING
        if settings.llm_api_key
        else "已收到您的问题，客服将结合企业知识库继续核验并回复。"
    )
    ticket = SupportTicket(
        requester_id=current_user.id,
        conversation_id=linked_conversation.id if linked_conversation else None,
        customer_name=payload.customer_name.strip(),
        question=payload.question.strip(),
        category=category,
        priority=payload.priority,
        suggested_reply=initial_reply,
        quality_score=0.0 if settings.llm_api_key else 0.74,
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    await ticket_event_broker.publish(_ticket_event("created", ticket))
    # A fresh session prevents use-after-close of the request-scoped session;
    # the detached task lets the 201 response return before model generation.
    if settings.llm_api_key:
        _schedule_ticket_enrichment(ticket.id, ticket.question)
    return ticket


@router.get("/support/tickets/events", tags=["support"])
async def stream_ticket_events(
    request: Request,
    current_user: User = Depends(require_roles("support_agent", "admin")),
) -> StreamingResponse:
    del current_user

    async def event_stream():
        def support_ticket(event: dict[str, object]) -> bool:
            return event.get("kind") == "ticket"

        async with ticket_event_broker.subscribe(support_ticket) as queue:
            yield "event: ready\ndata: {}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    if await request.is_disconnected():
                        return
                    yield ": keep-alive\n\n"
                    continue
                yield f"event: ticket\ndata: {json.dumps(_public_ticket_event(event), ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/support/tickets/mine/events", tags=["support"])
async def stream_my_ticket_events(
    request: Request,
    current_user: User = Depends(require_roles("enterprise_user")),
) -> StreamingResponse:
    requester_id = current_user.id

    async def event_stream():
        def owned_ticket(event: dict[str, object]) -> bool:
            return event.get("kind") == "ticket" and event.get("requester_id") == requester_id

        async with ticket_event_broker.subscribe(owned_ticket) as queue:
            yield "event: ready\ndata: {}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    if await request.is_disconnected():
                        return
                    yield ": keep-alive\n\n"
                    continue
                yield f"event: ticket\ndata: {json.dumps(_public_ticket_event(event), ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/support/tickets/{ticket_id}", response_model=TicketOut, tags=["support"])
def get_ticket(
    ticket_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SupportTicket:
    ticket = db.get(SupportTicket, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="工单不存在")
    if current_user.role == "enterprise_user":
        if ticket.requester_id != current_user.id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权访问该工单")
    elif current_user.role not in {"support_agent", "admin"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="当前角色无权访问工单")
    return ticket


@router.get("/support/conversations", response_model=list[ConversationOut], tags=["support"])
def list_handoff_conversations(
    status_filter: str | None = Query(default=None, alias="status"),
    current_user: User = Depends(require_roles("support_agent", "admin")),
    db: Session = Depends(get_db),
) -> list[ConversationOut]:
    """Return the support agent's multi-customer live conversation queue.

    By default only requested/active handoffs are shown, so a resolved human
    conversation cannot remain in the pending queue.  ``status=all`` (or
    ``status=closed``) is available for conversation-record management.
    """
    allowed_statuses = {"all", "pending", "requested", "active", "closed"}
    if status_filter is not None and status_filter not in allowed_statuses:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="不支持的会话状态筛选",
        )
    statement = select(Conversation)
    if status_filter in {None, "pending"}:
        statement = statement.where(Conversation.handoff_status.in_(("requested", "active")))
    elif status_filter == "all":
        statement = statement.where(Conversation.handoff_status.in_(("requested", "active", "closed")))
    elif status_filter in {"requested", "active", "closed"}:
        statement = statement.where(Conversation.handoff_status == status_filter)
    if current_user.role == "support_agent":
        statement = statement.where(
            (Conversation.assigned_agent_id.is_(None)) | (Conversation.assigned_agent_id == current_user.id)
        )
    conversations = list(db.scalars(statement.order_by(Conversation.updated_at.desc())).all())
    return [_conversation_response(db, conversation) for conversation in conversations]


@router.get(
    "/support/conversations/{conversation_id}/messages",
    response_model=list[MessageOut],
    tags=["support"],
)
def list_handoff_messages(
    conversation_id: int,
    include_closed: bool = Query(default=True),
    current_user: User = Depends(require_roles("support_agent", "admin")),
    db: Session = Depends(get_db),
) -> list[Message]:
    conversation = _support_conversation(db, conversation_id, current_user, allow_closed=include_closed)
    statement = select(Message).where(Message.conversation_id == conversation.id).order_by(Message.created_at.asc())
    return list(db.scalars(statement).all())


@router.post(
    "/support/conversations/{conversation_id}/messages",
    response_model=MessageOut,
    tags=["support"],
)
async def send_agent_message(
    conversation_id: int,
    payload: ConversationMessageCreate,
    current_user: User = Depends(require_roles("support_agent", "admin")),
    db: Session = Depends(get_db),
) -> MessageOut:
    conversation = _support_conversation(db, conversation_id, current_user)
    if current_user.role == "support_agent" and conversation.takeover_by_id is not None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="经营管理者已接管该会话，客服当前仅接收通知")
    if current_user.role == "support_agent":
        conversation.assigned_agent_id = current_user.id
    conversation.handoff_status = "active"
    message = _conversation_message(db, conversation, "agent", payload.content)
    db.commit()
    db.refresh(conversation)
    db.refresh(message)
    await ticket_event_broker.publish(_conversation_event("message", conversation, message))
    return MessageOut.model_validate(message)


def _validate_conversation_assignment(
    db: Session,
    requested_agent_id: int | None,
    current_user: User,
) -> User | None:
    """Validate an assignment without allowing an agent to impersonate peers."""
    if current_user.role == "support_agent" and requested_agent_id not in {None, current_user.id}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="客服只能分配给自己或取消分配")
    if requested_agent_id is None:
        return None
    agent = db.get(User, requested_agent_id)
    if agent is None or agent.role != "support_agent" or not agent.is_active:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="指定的客服不存在或已停用")
    return agent


@router.patch(
    "/support/conversations/{conversation_id}/assign",
    response_model=ConversationOut,
    tags=["support"],
)
@router.patch(
    "/support/conversations/{conversation_id}/assignment",
    response_model=ConversationOut,
    include_in_schema=False,
    tags=["support"],
)
@router.post(
    "/support/conversations/{conversation_id}/assign",
    response_model=ConversationOut,
    include_in_schema=False,
    tags=["support"],
)
async def assign_handoff_conversation(
    conversation_id: int,
    payload: ConversationAssignmentUpdate,
    current_user: User = Depends(require_roles("support_agent", "admin")),
    db: Session = Depends(get_db),
) -> ConversationOut:
    """Claim, reassign (admin), or release a live handoff conversation."""
    conversation = _support_conversation(db, conversation_id, current_user)
    _ensure_support_control_allowed(conversation, current_user)
    # An omitted field is the natural payload for the support UI's
    # "take over" action.  An explicit null remains the release operation.
    requested_agent_id = payload.assigned_agent_id
    if "assigned_agent_id" not in payload.model_fields_set:
        if current_user.role != "support_agent":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="管理员请明确指定接待的客服人员",
            )
        requested_agent_id = current_user.id
    _validate_conversation_assignment(db, requested_agent_id, current_user)
    conversation.assigned_agent_id = requested_agent_id
    conversation.handoff_status = "active" if requested_agent_id is not None else "requested"
    conversation.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(conversation)
    await ticket_event_broker.publish(_conversation_event("assignment", conversation))
    return _conversation_response(db, conversation)


async def _end_handoff_conversation(
    conversation_id: int,
    current_user: User,
    db: Session,
) -> ConversationOut:
    conversation = _support_conversation(db, conversation_id, current_user, allow_closed=True)
    _ensure_support_control_allowed(conversation, current_user)
    if conversation.handoff_status != "closed":
        conversation.handoff_status = "closed"
        message = _conversation_message(db, conversation, "system", "客服已结束本次会话。")
        db.commit()
        db.refresh(conversation)
        db.refresh(message)
        await ticket_event_broker.publish(_conversation_event("closed", conversation, message))
    return _conversation_response(db, conversation)


@router.post(
    "/support/conversations/{conversation_id}/end",
    response_model=ConversationOut,
    tags=["support"],
)
@router.post(
    "/support/conversations/{conversation_id}/close",
    response_model=ConversationOut,
    include_in_schema=False,
    tags=["support"],
)
async def end_handoff_conversation(
    conversation_id: int,
    current_user: User = Depends(require_roles("support_agent", "admin")),
    db: Session = Depends(get_db),
) -> ConversationOut:
    """Close a live human handoff and remove it from the pending queue."""
    return await _end_handoff_conversation(conversation_id, current_user, db)


@router.patch(
    "/support/conversations/{conversation_id}/status",
    response_model=ConversationOut,
    tags=["support"],
)
async def update_handoff_status(
    conversation_id: int,
    payload: ConversationStatusUpdate,
    current_user: User = Depends(require_roles("support_agent", "admin")),
    db: Session = Depends(get_db),
) -> ConversationOut:
    """Explicit status transition endpoint for clients that use a status menu."""
    if payload.status == "closed":
        return await _end_handoff_conversation(conversation_id, current_user, db)
    conversation = _support_conversation(db, conversation_id, current_user)
    _ensure_support_control_allowed(conversation, current_user)
    if payload.status == "active" and conversation.assigned_agent_id is None:
        conversation.assigned_agent_id = current_user.id if current_user.role == "support_agent" else None
        if conversation.assigned_agent_id is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="激活会话前必须先分配客服")
    if payload.status == "requested" and current_user.role == "support_agent":
        # Releasing a claimed session is the only requested transition an
        # individual agent may perform; it returns the row to the shared queue.
        if conversation.assigned_agent_id not in {None, current_user.id}:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="该会话已分配给其他客服")
        conversation.assigned_agent_id = None
    conversation.handoff_status = payload.status
    conversation.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(conversation)
    await ticket_event_broker.publish(_conversation_event("status", conversation))
    return _conversation_response(db, conversation)


@router.post(
    "/support/conversations/{conversation_id}/read",
    response_model=ConversationOut,
    tags=["support"],
)
async def mark_handoff_conversation_read(
    conversation_id: int,
    current_user: User = Depends(require_roles("support_agent", "admin")),
    db: Session = Depends(get_db),
) -> ConversationOut:
    """Acknowledge the current customer messages for the queue badge.

    The demo schema has no separate read-receipt table.  Marking read is
    represented by appending a system acknowledgement, which gives the
    deterministic queue projection a stable boundary and is visible in the
    conversation audit trail.
    """
    conversation = _support_conversation(db, conversation_id, current_user)
    _conversation_message(db, conversation, "system", "客服已查看最新消息。")
    db.commit()
    db.refresh(conversation)
    await ticket_event_broker.publish(_conversation_event("read", conversation))
    return _conversation_response(db, conversation)


@router.patch(
    "/support/conversations/{conversation_id}",
    response_model=ConversationOut,
    tags=["support"],
)
async def assign_handoff_conversation_legacy_path(
    conversation_id: int,
    payload: ConversationAssignmentUpdate,
    current_user: User = Depends(require_roles("support_agent", "admin")),
    db: Session = Depends(get_db),
) -> ConversationOut:
    """Compatibility path used by older clients for assignment actions."""
    return await assign_handoff_conversation(conversation_id, payload, current_user, db)


@router.get("/support/conversations/events", tags=["support"])
async def stream_handoff_events(
    request: Request,
    current_user: User = Depends(require_roles("support_agent", "admin")),
) -> StreamingResponse:
    is_admin = current_user.role == "admin"

    async def event_stream():
        def support_scope(event: dict[str, object]) -> bool:
            if event.get("kind") == "notification":
                return is_admin or event.get("recipient_id") == current_user.id
            if event.get("kind") != "conversation":
                return False
            if is_admin:
                return True
            return event.get("status") in {"requested", "active"} and event.get("assigned_agent_id") in {
                None,
                current_user.id,
            }

        async with ticket_event_broker.subscribe(support_scope) as queue:
            yield "event: ready\ndata: {}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    if await request.is_disconnected():
                        return
                    yield ": keep-alive\n\n"
                    continue
                event_name = "notification" if event.get("kind") == "notification" else "conversation"
                yield f"event: {event_name}\ndata: {json.dumps(_public_support_conversation_event(event), ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get(
    "/support/conversations/{conversation_id}",
    response_model=ConversationOut,
    tags=["support"],
)
def get_handoff_conversation(
    conversation_id: int,
    current_user: User = Depends(require_roles("support_agent", "admin")),
    db: Session = Depends(get_db),
) -> ConversationOut:
    conversation = _support_conversation(db, conversation_id, current_user, allow_closed=True)
    return _conversation_response(db, conversation)


@router.get("/executive/support-agents", response_model=list[UserOut], tags=["executive"])
@router.get("/support/agents", response_model=list[UserOut], include_in_schema=False, tags=["support"])
def list_support_agents(
    current_user: User = Depends(require_roles("executive", "admin")),
    db: Session = Depends(get_db),
) -> list[User]:
    """Return active support agents available for a management notice."""
    del current_user
    return list(
        db.scalars(
            select(User)
            .where(User.role == "support_agent", User.is_active.is_(True))
            .order_by(User.display_name.asc(), User.id.asc())
        ).all()
    )


def _notification_for_agent(
    db: Session,
    *,
    recipient_id: int,
    sender_id: int,
    conversation_id: int | None,
    content: str,
    kind: str = "executive_takeover",
) -> SupportNotification:
    notification = SupportNotification(
        recipient_id=recipient_id,
        sender_id=sender_id,
        conversation_id=conversation_id,
        content=content.strip(),
        kind=kind,
    )
    db.add(notification)
    db.flush()
    return notification


@router.post(
    "/executive/conversations/{conversation_id}/takeover",
    response_model=ConversationOut,
    tags=["executive"],
)
@router.post(
    "/admin/conversations/{conversation_id}/takeover",
    response_model=ConversationOut,
    include_in_schema=False,
    tags=["admin"],
)
async def executive_takeover_conversation(
    conversation_id: int,
    payload: ExecutiveTakeoverRequest,
    current_user: User = Depends(require_roles("executive", "admin")),
    db: Session = Depends(get_db),
) -> ConversationOut:
    """Force management control of a live conversation and notify one agent."""
    conversation = _executive_conversation(db, conversation_id, current_user)
    agent = db.get(User, payload.assigned_agent_id)
    if agent is None or agent.role != "support_agent" or not agent.is_active:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="指定的客服不存在或已停用")
    notice = (payload.notice or "经营管理者已接管此会话，请客服留意并接收后续通知。 ").strip()
    conversation.assigned_agent_id = agent.id
    conversation.takeover_by_id = current_user.id
    conversation.takeover_notice = notice
    conversation.takeover_at = datetime.now(timezone.utc)
    conversation.handoff_status = "active"
    message = _conversation_message(
        db,
        conversation,
        "system",
        f"经营管理者已强制接管会话，并通知客服 {agent.display_name}：{notice}",
    )
    notification = _notification_for_agent(
        db,
        recipient_id=agent.id,
        sender_id=current_user.id,
        conversation_id=conversation.id,
        content=notice,
    )
    db.commit()
    db.refresh(conversation)
    db.refresh(message)
    db.refresh(notification)
    await ticket_event_broker.publish(_conversation_event("executive_takeover", conversation, message))
    await ticket_event_broker.publish(_notification_event("created", notification, conversation=conversation))
    return _conversation_response(db, conversation)


@router.post(
    "/executive/conversations/{conversation_id}/notify",
    response_model=SupportNotificationOut,
    tags=["executive"],
)
@router.post(
    "/support/conversations/{conversation_id}/notify",
    response_model=SupportNotificationOut,
    include_in_schema=False,
    tags=["support"],
)
async def executive_notify_support_agent(
    conversation_id: int,
    payload: ExecutiveNotificationRequest,
    current_user: User = Depends(require_roles("executive", "admin")),
    db: Session = Depends(get_db),
) -> SupportNotificationOut:
    """Send a targeted notice without changing the current conversation owner."""
    conversation = _executive_conversation(db, conversation_id, current_user)
    agent_id = payload.resolved_agent_id()
    if agent_id is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="必须指定客服")
    agent = db.get(User, agent_id)
    if agent is None or agent.role != "support_agent" or not agent.is_active:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="指定的客服不存在或已停用")
    content = (payload.notice or "请关注该客服会话并接收管理通知。 ").strip()
    content = payload.resolved_message() or content
    notification = _notification_for_agent(
        db,
        recipient_id=agent.id,
        sender_id=current_user.id,
        conversation_id=conversation.id,
        content=content,
        kind="executive_notice",
    )
    db.commit()
    db.refresh(notification)
    await ticket_event_broker.publish(_notification_event("created", notification, conversation=conversation))
    return SupportNotificationOut.model_validate(notification)


@router.get(
    "/executive/conversations",
    response_model=list[ConversationOut],
    tags=["executive"],
)
def list_executive_conversations(
    status_filter: str | None = Query(default=None, alias="status"),
    current_user: User = Depends(require_roles("executive", "admin")),
    db: Session = Depends(get_db),
) -> list[ConversationOut]:
    del current_user
    allowed = {None, "all", "pending", "requested", "active", "closed"}
    if status_filter not in allowed:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="不支持的会话状态筛选")
    statement = select(Conversation)
    if status_filter in {None, "pending"}:
        statement = statement.where(Conversation.handoff_status.in_(("requested", "active")))
    elif status_filter in {"requested", "active", "closed"}:
        statement = statement.where(Conversation.handoff_status == status_filter)
    conversations = list(db.scalars(statement.order_by(Conversation.updated_at.desc())).all())
    return [_conversation_response(db, conversation) for conversation in conversations]


@router.get(
    "/executive/conversations/{conversation_id}",
    response_model=ConversationOut,
    tags=["executive"],
)
def get_executive_conversation(
    conversation_id: int,
    current_user: User = Depends(require_roles("executive", "admin")),
    db: Session = Depends(get_db),
) -> ConversationOut:
    conversation = _executive_conversation(db, conversation_id, current_user, allow_closed=True)
    return _conversation_response(db, conversation)


@router.get(
    "/executive/conversations/{conversation_id}/messages",
    response_model=list[MessageOut],
    tags=["executive"],
)
def list_executive_messages(
    conversation_id: int,
    current_user: User = Depends(require_roles("executive", "admin")),
    db: Session = Depends(get_db),
) -> list[Message]:
    conversation = _executive_conversation(db, conversation_id, current_user, allow_closed=True)
    return list(
        db.scalars(
            select(Message)
            .where(Message.conversation_id == conversation.id)
            .order_by(Message.created_at.asc(), Message.id.asc())
        ).all()
    )


@router.post(
    "/executive/conversations/{conversation_id}/messages",
    response_model=MessageOut,
    tags=["executive"],
)
async def send_executive_message(
    conversation_id: int,
    payload: ConversationMessageCreate,
    current_user: User = Depends(require_roles("executive", "admin")),
    db: Session = Depends(get_db),
) -> MessageOut:
    conversation = _executive_conversation(db, conversation_id, current_user)
    if conversation.takeover_by_id != current_user.id and current_user.role != "admin":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="请先接管该会话后再发送消息")
    message = _conversation_message(db, conversation, "executive", payload.content)
    conversation.handoff_status = "active"
    db.commit()
    db.refresh(conversation)
    db.refresh(message)
    await ticket_event_broker.publish(_conversation_event("message", conversation, message))
    return MessageOut.model_validate(message)


@router.get(
    "/support/notifications",
    response_model=list[SupportNotificationOut],
    tags=["support"],
)
def list_support_notifications(
    unread_only: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=500),
    current_user: User = Depends(require_roles("support_agent", "admin")),
    db: Session = Depends(get_db),
) -> list[SupportNotification]:
    statement = select(SupportNotification)
    if current_user.role == "support_agent":
        statement = statement.where(SupportNotification.recipient_id == current_user.id)
    if unread_only:
        statement = statement.where(SupportNotification.is_read.is_(False))
    return list(
        db.scalars(
            statement.order_by(SupportNotification.created_at.desc(), SupportNotification.id.desc()).limit(limit)
        ).all()
    )


@router.post(
    "/support/notifications/{notification_id}/read",
    response_model=SupportNotificationOut,
    tags=["support"],
)
async def mark_support_notification_read(
    notification_id: int,
    current_user: User = Depends(require_roles("support_agent", "admin")),
    db: Session = Depends(get_db),
) -> SupportNotificationOut:
    notification = db.get(SupportNotification, notification_id)
    if notification is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="通知不存在")
    if current_user.role == "support_agent" and notification.recipient_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权读取该通知")
    notification.is_read = True
    notification.read_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(notification)
    await ticket_event_broker.publish(_notification_event("read", notification))
    return SupportNotificationOut.model_validate(notification)


@router.get("/support/notifications/events", tags=["support"])
async def stream_support_notifications(
    request: Request,
    current_user: User = Depends(require_roles("support_agent", "admin")),
) -> StreamingResponse:
    """Stream only notices addressed to the connected support agent."""
    is_admin = current_user.role == "admin"

    async def event_stream():
        def notification_scope(event: dict[str, object]) -> bool:
            return event.get("kind") == "notification" and (
                is_admin or event.get("recipient_id") == current_user.id
            )

        async with ticket_event_broker.subscribe(notification_scope) as queue:
            yield "event: ready\ndata: {}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    if await request.is_disconnected():
                        return
                    yield ": keep-alive\n\n"
                    continue
                yield f"event: notification\ndata: {json.dumps(_public_support_conversation_event(event), ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.patch("/support/tickets/{ticket_id}", response_model=TicketOut, tags=["support"])
async def update_ticket(
    ticket_id: int,
    payload: TicketUpdate,
    current_user: User = Depends(require_roles("support_agent", "admin")),
    db: Session = Depends(get_db),
) -> SupportTicket:
    ticket = db.get(SupportTicket, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="工单不存在")
    if payload.status is not None:
        allowed = _TICKET_TRANSITIONS.get(ticket.status, frozenset())
        if payload.status not in allowed:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="工单状态已变化，请刷新后重试")
        if payload.status == "resolved":
            final_reply = (payload.final_reply if payload.final_reply is not None else ticket.final_reply or "").strip()
            if not final_reply:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="确认解决前必须填写最终回复")
        ticket.status = payload.status
    if payload.final_reply is not None:
        ticket.final_reply = payload.final_reply.strip()
    db.commit()
    db.refresh(ticket)
    await ticket_event_broker.publish(_ticket_event("updated", ticket))
    return ticket


@router.get("/admin/users", response_model=list[UserOut], tags=["admin"])
def list_users(
    q: str | None = Query(default=None, max_length=100),
    role: str | None = Query(default=None),
    is_active: bool | None = Query(default=None),
    include_deleted: bool = Query(default=False),
    current_user: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> list[User]:
    del current_user
    stmt = select(User).order_by(User.created_at.desc())
    if not include_deleted:
        stmt = stmt.where(User.deleted_at.is_(None))
    if q:
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(User.display_name.ilike(pattern) | User.email.ilike(pattern))
    if role:
        stmt = stmt.where(User.role == role)
    if is_active is not None:
        stmt = stmt.where(User.is_active.is_(is_active))
    return list(db.scalars(stmt).all())


@router.post("/admin/users", response_model=UserOut, status_code=status.HTTP_201_CREATED, tags=["admin"])
def create_user(
    payload: UserCreate,
    current_user: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> User:
    email_normalized = payload.email.strip().lower()
    existing = db.scalar(select(User).where(User.email == email_normalized))
    if existing is not None:
        record_admin_action(
            db, current_user, "create_user",
            target_type="user", target_name=email_normalized,
            detail=f"角色={payload.role}", success=False, error_message="邮箱已存在",
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该邮箱已被注册")
    user = User(
        email=email_normalized,
        password_hash=hash_password(payload.password),
        display_name=payload.display_name.strip(),
        role=payload.role,
        is_active=payload.is_active,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    record_admin_action(
        db, current_user, "create_user",
        target_type="user", target_id=user.id, target_name=user.display_name,
        detail=f"邮箱={user.email}, 角色={user.role}",
    )
    return user


@router.post("/admin/users/{user_id}/reset-password", response_model=UserOut, tags=["admin"])
def reset_user_password(
    user_id: int,
    payload: UserResetPassword,
    current_user: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")
    user.password_hash = hash_password(payload.new_password)
    db.commit()
    db.refresh(user)
    record_admin_action(
        db, current_user, "reset_password",
        target_type="user", target_id=user.id, target_name=user.display_name,
        detail="管理员重置密码",
    )
    return user


@router.patch("/admin/users/{user_id}", response_model=UserOut, tags=["admin"])
def update_user(
    user_id: int,
    payload: UserRoleUpdate,
    current_user: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")
    # Self-protection: admin cannot deactivate or demote themselves.
    if user.id == current_user.id and (not payload.is_active or payload.role != "admin"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="不能停用或降级当前登录的管理员账户",
        )
    removes_active_admin = user.role == "admin" and user.is_active and (
        payload.role != "admin" or not payload.is_active
    )
    if removes_active_admin:
        active_admin_count = db.scalar(
            select(func.count(User.id)).where(
                User.role == "admin", User.is_active.is_(True), User.deleted_at.is_(None)
            )
        ) or 0
        if active_admin_count <= 1:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="系统必须保留至少一个已启用的管理员账户",
            )
    changes: list[str] = []
    if user.role != payload.role:
        changes.append(f"角色: {user.role} → {payload.role}")
    if user.is_active != payload.is_active:
        changes.append(f"状态: {'启用' if user.is_active else '停用'} → {'启用' if payload.is_active else '停用'}")
    user.role = payload.role
    user.is_active = payload.is_active
    db.commit()
    db.refresh(user)
    if changes:
        record_admin_action(
            db, current_user, "update_user",
            target_type="user", target_id=user.id, target_name=user.display_name,
            detail="; ".join(changes),
        )
    return user


@router.delete("/admin/users/{user_id}", response_model=UserOut, tags=["admin"])
def delete_user(
    user_id: int,
    current_user: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> User:
    """Soft-delete a user. Data (conversations, tickets, messages) is preserved."""
    user = db.get(User, user_id)
    if user is None or user.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")
    if user.id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="不能删除当前登录的管理员账户",
        )
    if user.role == "admin" and user.is_active:
        active_admin_count = db.scalar(
            select(func.count(User.id)).where(
                User.role == "admin", User.is_active.is_(True), User.deleted_at.is_(None)
            )
        ) or 0
        if active_admin_count <= 1:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="系统必须保留至少一个已启用的管理员账户",
            )
    user.deleted_at = datetime.now(timezone.utc)
    user.is_active = False
    db.commit()
    db.refresh(user)
    record_admin_action(
        db, current_user, "delete_user",
        target_type="user", target_id=user.id, target_name=user.display_name,
        detail=f"软删除用户，邮箱={user.email}",
    )
    return user


@router.get("/admin/settings", response_model=list[SettingOut], tags=["admin"])
def list_settings(
    current_user: User = Depends(require_roles("admin")), db: Session = Depends(get_db)
) -> list[AISetting]:
    del current_user
    return list(db.scalars(select(AISetting).order_by(AISetting.key)).all())


@router.put("/admin/settings/{key}", response_model=SettingOut, tags=["admin"])
def update_setting(
    key: str,
    payload: SettingUpdate,
    current_user: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> AISetting:
    try:
        value = validate_setting(key, payload.value)
    except ValueError as error:
        record_admin_action(
            db, current_user, "update_setting",
            target_type="setting", target_name=key,
            detail=f"值={payload.value[:100]}", success=False, error_message=str(error),
        )
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error
    setting = db.scalar(select(AISetting).where(AISetting.key == key))
    old_value = setting.value if setting else "(新建)"
    if setting is None:
        setting = AISetting(key=key, value=value, description=payload.description.strip())
        db.add(setting)
    else:
        setting.value = value
        setting.description = payload.description.strip()
    db.commit()
    db.refresh(setting)
    retrieval_cache.clear()
    record_admin_action(
        db, current_user, "update_setting",
        target_type="setting", target_name=key,
        detail=f"{old_value[:60]} → {value[:60]}",
    )
    return setting


@router.put("/admin/settings-reset", response_model=list[SettingOut], tags=["admin"])
def reset_settings(
    current_user: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> list[AISetting]:
    """Restore all AI settings to their factory defaults."""
    results: list[AISetting] = []
    for key, default_value in SETTING_DEFAULTS.items():
        setting = db.scalar(select(AISetting).where(AISetting.key == key))
        if setting is None:
            setting = AISetting(key=key, value=default_value, description=SETTING_DESCRIPTIONS.get(key, ""))
            db.add(setting)
        else:
            setting.value = default_value
            setting.description = SETTING_DESCRIPTIONS.get(key, "")
        results.append(setting)
    db.commit()
    for setting in results:
        db.refresh(setting)
    retrieval_cache.clear()
    record_admin_action(
        db, current_user, "reset_settings",
        target_type="setting", target_name="全部配置",
        detail=f"恢复 {len(SETTING_DEFAULTS)} 项配置为默认值",
    )
    return results


@router.get("/admin/audit-logs", response_model=AdminAuditLogPage, tags=["admin"])
def list_audit_logs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    action: str | None = Query(default=None),
    current_user: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> AdminAuditLogPage:
    del current_user
    stmt = select(AdminAuditLog).order_by(AdminAuditLog.created_at.desc())
    count_stmt = select(func.count(AdminAuditLog.id))
    if action:
        stmt = stmt.where(AdminAuditLog.action == action)
        count_stmt = count_stmt.where(AdminAuditLog.action == action)
    total = db.scalar(count_stmt) or 0
    items = list(db.scalars(stmt.offset((page - 1) * page_size).limit(page_size)).all())
    return AdminAuditLogPage(
        items=[AdminAuditLogOut.model_validate(item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/admin/conversations",
    response_model=list[ConversationAuditSummary],
    tags=["admin"],
)
def list_admin_conversations(
    current_user: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> list[ConversationAuditSummary]:
    """List every conversation as one compact row for the audit accordion."""
    del current_user
    conversations = db.scalars(select(Conversation).order_by(Conversation.updated_at.desc())).all()
    return [_conversation_audit_summary(db, conversation) for conversation in conversations]


@router.get("/admin/messages", tags=["admin"])
def recent_messages(
    current_user: User = Depends(require_roles("admin")), db: Session = Depends(get_db)
) -> list[dict[str, object]]:
    del current_user
    messages = db.scalars(select(Message).order_by(Message.created_at.desc()).limit(80)).all()
    return [_audit_message_payload(message) for message in messages]


@router.get(
    "/admin/conversations/{conversation_id}",
    response_model=ConversationAuditDetail,
    tags=["admin"],
)
def get_admin_conversation(
    conversation_id: int,
    current_user: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> ConversationAuditDetail:
    """Return any user's full transcript for administrator audit."""
    del current_user
    conversation = db.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")
    return ConversationAuditDetail.model_validate(
        _conversation_audit_payload(db, conversation, include_messages=True)
    )


@router.get("/dashboard/overview", tags=["dashboard"])
def dashboard_overview(
    current_user: User = Depends(require_roles("admin", "executive")), db: Session = Depends(get_db)
) -> dict[str, object]:
    del current_user
    ticket_count = db.scalar(select(func.count(SupportTicket.id))) or 0
    open_count = db.scalar(select(func.count(SupportTicket.id)).where(SupportTicket.status != "resolved")) or 0
    message_count = db.scalar(select(func.count(Message.id)).where(Message.role == "user")) or 0
    average_quality = db.scalar(select(func.avg(SupportTicket.quality_score))) or 0.0
    feedback_count = int(
        db.scalar(
            select(func.count(Conversation.id)).where(Conversation.feedback_submitted_at.is_not(None))
        )
        or 0
    )
    average_feedback_rating = db.scalar(
        select(func.avg(Conversation.feedback_rating)).where(Conversation.feedback_submitted_at.is_not(None))
    )
    helpful_feedback_count = int(
        db.scalar(
            select(func.count(Conversation.id)).where(
                Conversation.feedback_submitted_at.is_not(None),
                Conversation.feedback_helpful.is_(True),
            )
        )
        or 0
    )
    category_rows = db.execute(
        select(SupportTicket.category, func.count(SupportTicket.id)).group_by(SupportTicket.category)
    ).all()
    category_distribution = [{"name": category, "value": count} for category, count in category_rows]
    today = datetime.now(timezone.utc).date()
    current_start = datetime.combine(today - timedelta(days=6), datetime.min.time(), tzinfo=timezone.utc)
    current_end = datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    previous_start = current_start - timedelta(days=7)

    def count_messages(start: datetime, end: datetime) -> int:
        return int(
            db.scalar(
                select(func.count(Message.id)).where(
                    Message.role == "user",
                    Message.created_at >= start,
                    Message.created_at < end,
                )
            )
            or 0
        )

    def count_tickets(start: datetime, end: datetime) -> int:
        return int(
            db.scalar(
                select(func.count(SupportTicket.id)).where(
                    SupportTicket.created_at >= start,
                    SupportTicket.created_at < end,
                )
            )
            or 0
        )

    current_ticket_count = count_tickets(current_start, current_end)
    current_consultations = current_ticket_count + count_messages(current_start, current_end)
    previous_consultations = count_tickets(previous_start, current_start) + count_messages(
        previous_start, current_start
    )
    consultation_delta = (
        f"近7日 {current_consultations} 条；较前7日 {current_consultations - previous_consultations:+d} 条"
        if previous_consultations
        else f"近7日 {current_consultations} 条；暂无历史对比"
    )

    current_quality, current_quality_count = db.execute(
        select(func.avg(SupportTicket.quality_score), func.count(SupportTicket.id)).where(
            SupportTicket.created_at >= current_start,
            SupportTicket.created_at < current_end,
        )
    ).one()
    previous_quality, previous_quality_count = db.execute(
        select(func.avg(SupportTicket.quality_score), func.count(SupportTicket.id)).where(
            SupportTicket.created_at >= previous_start,
            SupportTicket.created_at < current_start,
        )
    ).one()
    if not current_quality_count:
        quality_delta = "近7日暂无质检记录"
    elif previous_quality_count:
        difference = (float(current_quality) - float(previous_quality)) * 100
        quality_delta = (
            f"近7日均值 {float(current_quality) * 100:.0f}%；"
            f"较前7日 {difference:+.1f} 个百分点"
        )
    else:
        quality_delta = f"近7日均值 {float(current_quality) * 100:.0f}%；暂无历史对比"

    trend_rows = db.execute(
        select(
            func.date(SupportTicket.created_at),
            func.avg(SupportTicket.quality_score),
        )
        .where(
            SupportTicket.created_at >= current_start,
            SupportTicket.created_at < current_end,
        )
        .group_by(func.date(SupportTicket.created_at))
        .order_by(func.date(SupportTicket.created_at))
    ).all()
    trend = [
        {
            "date": datetime.strptime(str(day), "%Y-%m-%d").strftime("%m-%d"),
            "value": round(float(day_average) * 100, 1),
        }
        for day, day_average in trend_rows
        if day is not None and day_average is not None
    ]
    feedback_trend_rows = db.execute(
        select(
            func.date(Conversation.feedback_submitted_at),
            func.avg(Conversation.feedback_rating),
        )
        .where(Conversation.feedback_submitted_at.is_not(None))
        .group_by(func.date(Conversation.feedback_submitted_at))
        .order_by(func.date(Conversation.feedback_submitted_at))
    ).all()
    feedback_trend = [
        {
            "date": datetime.strptime(str(day), "%Y-%m-%d").strftime("%m-%d"),
            "value": round(float(day_average) / 5 * 100, 1),
        }
        for day, day_average in feedback_trend_rows
        if day is not None and day_average is not None
    ]
    top_category = max(category_distribution, key=lambda item: item["value"], default={"name": "暂无", "value": 0})
    high_priority_open = db.scalar(
        select(func.count(SupportTicket.id)).where(
            SupportTicket.status != "resolved",
            SupportTicket.priority.in_(["high", "urgent"]),
        )
    ) or 0
    document_count = db.scalar(select(func.count(KnowledgeDocument.id))) or 0
    ready_document_count = db.scalar(
        select(func.count(KnowledgeDocument.id)).where(KnowledgeDocument.status == "ready")
    ) or 0
    status_counts = {
        "total": int(ticket_count),
        "pending": int(db.scalar(select(func.count(SupportTicket.id)).where(SupportTicket.status == "open")) or 0),
        "open": int(db.scalar(select(func.count(SupportTicket.id)).where(SupportTicket.status == "open")) or 0),
        "in_progress": int(db.scalar(select(func.count(SupportTicket.id)).where(SupportTicket.status == "in_progress")) or 0),
        "resolved": int(db.scalar(select(func.count(SupportTicket.id)).where(SupportTicket.status == "resolved")) or 0),
        "urgent": int(db.scalar(select(func.count(SupportTicket.id)).where(SupportTicket.priority == "urgent")) or 0),
    }
    # ``quality_score`` is the existing deterministic proxy used by this demo;
    # expose it under an explicit satisfaction key so the dashboard does not
    # silently conflate it with the agent's internal quality label.
    satisfaction = round(float(average_quality) * 100, 1)
    actual_satisfaction = (
        round(float(average_feedback_rating) / 5 * 100, 1)
        if feedback_count and average_feedback_rating is not None
        else None
    )
    return {
        "metrics": [
            {
                "label": "累计咨询",
                "value": ticket_count + message_count,
                "delta": consultation_delta,
                "tone": "teal",
            },
            {
                "label": "待处理工单",
                "value": open_count,
                "delta": f"近7日新增 {current_ticket_count} 条",
                "tone": "coral",
            },
            {
                "label": "AI 建议质检代理分",
                "value": f"{average_quality * 100:.0f}%",
                "delta": quality_delta,
                "tone": "gold",
            },
            {
                "label": "知识库文档",
                "value": document_count,
                "delta": f"可检索 {ready_document_count}/{document_count} 篇",
                "tone": "blue",
            },
        ],
        "category_distribution": category_distribution,
        # Keep the old field for clients that still render the teaching proxy,
        # but prefer real user feedback once at least one rating exists.
        "satisfaction_trend": feedback_trend or trend,
        "feedback_satisfaction_trend": feedback_trend,
        "feedback_count": feedback_count,
        "feedback_helpful_rate": round(helpful_feedback_count / feedback_count * 100, 1) if feedback_count else None,
        "actual_ai_reply_satisfaction": actual_satisfaction,
        "consultation_count": int(ticket_count + message_count),
        "ai_reply_satisfaction": actual_satisfaction if actual_satisfaction is not None else satisfaction,
        "satisfaction": satisfaction,
        "ticket_statuses": status_counts,
        "ticket_counts": status_counts,
        "ticket_summary": status_counts,
        "urgent_tickets": status_counts["urgent"],
        "insights": [
            f"累计工单中高频咨询为“{top_category['name']}”，共 {top_category['value']} 条。",
            f"当前有 {open_count} 条待处理工单，其中高优先级或紧急工单 {high_priority_open} 条。",
            f"近7日记录 {current_consultations} 条咨询、{current_ticket_count} 条新工单。",
        ],
        "system": {
            "provider": "OpenAI compatible 已配置" if settings.llm_api_key else "本地演示模型",
            "dify": "已配置，调用时检查" if settings.dify_api_url and settings.dify_api_key else "未配置，按请求本地回退",
            "index": "本地余弦向量检索",
        },
    }


def _dashboard_ticket_row(ticket: SupportTicket) -> dict[str, object]:
    """Expose one ticket in the generic dashboard detail row contract."""
    payload = TicketOut.model_validate(ticket).model_dump(mode="json")
    payload.update(
        {
            "label": ticket.customer_name,
            "title": ticket.question,
            "content": ticket.question,
            "answer": ticket.final_reply or ticket.suggested_reply,
        }
    )
    return payload


def _dashboard_consultation_rows(db: Session, limit: int) -> list[dict[str, object]]:
    """Flatten persisted conversation messages with their customer context."""
    rows = db.execute(
        select(Message, Conversation)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(limit)
    ).all()
    owner_cache: dict[int, User | None] = {}
    result: list[dict[str, object]] = []
    for message, conversation in rows:
        if conversation.user_id not in owner_cache:
            owner_cache[conversation.user_id] = db.get(User, conversation.user_id)
        owner = owner_cache[conversation.user_id]
        audit_message = _audit_message_payload(message)
        result.append(
            {
                "id": message.id,
                "message_id": message.id,
                "conversation_id": conversation.id,
                "title": conversation.title,
                "label": conversation.title,
                "content": message.content,
                "question": message.content if message.role == "user" else None,
                "role": message.role,
                "sender_role": audit_message["sender_role"],
                "sender_label": audit_message["sender_label"],
                "display_role": audit_message["display_role"],
                "actor_role": audit_message["actor_role"],
                "created_at": message.created_at,
                "updated_at": conversation.updated_at,
                "status": conversation.handoff_status,
                "handoff_status": conversation.handoff_status,
                "customer_name": owner.display_name if owner is not None else conversation.title,
                "customer_email": owner.email if owner is not None else None,
                "trace": audit_message["trace"],
                "citations": audit_message["citations"],
            }
        )
    return result


def _dashboard_feedback_rows(db: Session, limit: int) -> list[dict[str, object]]:
    """Return the real end-of-conversation feedback behind satisfaction data."""
    rows = db.execute(
        select(Conversation, User)
        .join(User, User.id == Conversation.user_id)
        .where(Conversation.feedback_submitted_at.is_not(None))
        .order_by(Conversation.feedback_submitted_at.desc(), Conversation.id.desc())
        .limit(limit)
    ).all()
    return [
        {
            "id": conversation.id,
            "conversation_id": conversation.id,
            "title": conversation.title,
            "label": owner.display_name,
            "content": conversation.feedback_comment or ("有帮助" if conversation.feedback_helpful else "没有帮助"),
            "rating": conversation.feedback_rating,
            "helpful": conversation.feedback_helpful,
            "feedback_comment": conversation.feedback_comment,
            "feedback_submitted_at": conversation.feedback_submitted_at,
            "created_at": conversation.feedback_submitted_at,
            "customer_name": owner.display_name,
            "customer_email": owner.email,
        }
        for conversation, owner in rows
    ]


@router.get(
    "/dashboard/details",
    response_model=DashboardDetailsOut,
    tags=["dashboard"],
)
def dashboard_details(
    scope: DashboardDetailScope = Query(default="tickets"),
    limit: int = Query(default=200, ge=1, le=500),
    status_filter: str | None = Query(default=None, alias="status"),
    category_filter: str | None = Query(default=None, alias="category"),
    priority_filter: str | None = Query(default=None, alias="priority"),
    current_user: User = Depends(require_roles("admin", "executive")),
    db: Session = Depends(get_db),
) -> DashboardDetailsOut:
    """Return concrete rows behind a dashboard card or chart."""
    overview = dashboard_overview(current_user, db)
    ticket_statement = select(SupportTicket).order_by(SupportTicket.updated_at.desc(), SupportTicket.id.desc())
    if status_filter and status_filter != "all":
        if status_filter == "pending":
            # The dashboard's "待处理" card is the explicit open state;
            # keep in-progress work in its own detail bucket.
            ticket_statement = ticket_statement.where(SupportTicket.status == "open")
        elif status_filter in {"open", "in_progress", "resolved"}:
            ticket_statement = ticket_statement.where(SupportTicket.status == status_filter)
        elif status_filter != "all":
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="不支持的工单状态筛选")
    if category_filter:
        ticket_statement = ticket_statement.where(SupportTicket.category == category_filter)
    if priority_filter:
        if priority_filter not in {"low", "normal", "high", "urgent"}:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="不支持的工单优先级筛选")
        ticket_statement = ticket_statement.where(SupportTicket.priority == priority_filter)
    tickets = list(db.scalars(ticket_statement.limit(limit)).all())

    if scope == "tickets":
        title = "全部工单"
        rows = [_dashboard_ticket_row(ticket) for ticket in tickets]
        summary = f"共 {len(rows)} 条工单，按最近更新时间排列。"
    elif scope == "status":
        title = "工单状态明细"
        status_order = {"open": 0, "in_progress": 1, "resolved": 2}
        tickets.sort(key=lambda ticket: (status_order.get(ticket.status, 9), -(ticket.updated_at.timestamp() if ticket.updated_at else 0)))
        rows = [_dashboard_ticket_row(ticket) for ticket in tickets]
        summary = "按待处理、处理中和已解决状态查看工单明细。"
    elif scope == "category":
        title = "问题分类明细"
        tickets.sort(key=lambda ticket: (ticket.category, -(ticket.updated_at.timestamp() if ticket.updated_at else 0)))
        rows = [_dashboard_ticket_row(ticket) for ticket in tickets]
        summary = "按问题分类查看对应工单与客户问题。"
    elif scope == "satisfaction":
        title = "AI 回复满意度明细"
        rows = _dashboard_feedback_rows(db, limit)
        if rows:
            summary = f"共收集 {len(rows)} 条用户评价；评分按 1-5 分换算为满意度。"
        else:
            # Preserve the original audit rows for older clients while making
            # it explicit that they are not customer satisfaction feedback.
            tickets.sort(key=lambda ticket: (-(ticket.quality_score or 0), -(ticket.updated_at.timestamp() if ticket.updated_at else 0)))
            rows = [_dashboard_ticket_row(ticket) for ticket in tickets]
            summary = "暂无用户评价；以下仅保留工单质检代理分供核验。"
    elif scope == "consultations":
        title = "客户咨询明细"
        rows = _dashboard_consultation_rows(db, limit)
        summary = f"共 {len(rows)} 条会话消息，包含客户、AI、客服和系统角色。"
    else:  # insights
        title = "AI 智能分析洞察"
        insights = overview.get("insights", [])
        rows = [
            {
                "id": index + 1,
                "label": f"洞察 {index + 1}",
                "title": "运营洞察",
                "content": insight,
                "created_at": datetime.now(timezone.utc),
            }
            for index, insight in enumerate(insights[:limit])
        ]
        summary = f"当前生成 {len(rows)} 条运营洞察。"
    return DashboardDetailsOut(scope=scope, title=title, summary=summary, rows=rows)


@router.get(
    "/dashboard/conversations/{conversation_id}",
    response_model=ConversationAuditDetail,
    tags=["dashboard"],
)
def get_dashboard_conversation(
    conversation_id: int,
    current_user: User = Depends(require_roles("admin", "executive")),
    db: Session = Depends(get_db),
) -> ConversationAuditDetail:
    """Allow dashboard viewers to open a complete read-only transcript."""
    del current_user
    conversation = db.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")
    return ConversationAuditDetail.model_validate(
        _conversation_audit_payload(db, conversation, include_messages=True)
    )


@router.get("/dashboard/report", tags=["dashboard"])
def generate_report(
    current_user: User = Depends(require_roles("admin", "executive")), db: Session = Depends(get_db)
) -> dict[str, str]:
    del current_user
    unresolved = db.scalar(select(func.count(SupportTicket.id)).where(SupportTicket.status != "resolved")) or 0
    high_priority = db.scalar(
        select(func.count(SupportTicket.id)).where(
            SupportTicket.status != "resolved",
            SupportTicket.priority.in_(["high", "urgent"]),
        )
    ) or 0
    return {
        "title": "商务服务 AI 运营简报",
        "summary": (
            f"当前共有 {unresolved} 条未闭环工单，其中高优先级或紧急工单 {high_priority} 条。"
            "建议先处理故障与账户访问类请求，再根据知识检索命中情况补充高频 FAQ。"
        ),
    }


@router.get("/voice/capabilities", tags=["voice"])
def voice_capabilities(current_user: User = Depends(get_current_user)) -> dict[str, object]:
    del current_user
    return {
        "input": "浏览器 Web Speech API（客户端能力检测后启用）",
        "output": "浏览器 speechSynthesis（客户端本地朗读）",
        "fallback": "浏览器不支持时保留完整文本输入和回复流程",
    }


# ---------------------------------------------------------------------------
# Internal LangGraph callback (called by Dify router workflow HTTP node)
# ---------------------------------------------------------------------------


@router.post(
    "/tools/langgraph/run",
    response_model=LangGraphCallbackResponse,
    tags=["internal"],
    include_in_schema=False,
)
async def langgraph_callback(
    payload: LangGraphCallbackRequest,
    db: Session = Depends(get_db),
    x_dify_callback_secret: str | None = Header(default=None),
) -> LangGraphCallbackResponse:
    """Internal endpoint for Dify router workflow HTTP callback node.

    Security: verified via shared secret header, not user JWT.
    Recursion guard: route_depth > 1 is rejected.
    """
    started = time.monotonic()

    # 1. Verify shared secret.
    expected_secret = settings.dify_callback_secret
    if not expected_secret:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="回调服务未配置")
    if not x_dify_callback_secret or not secrets.compare_digest(x_dify_callback_secret, expected_secret):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="认证失败")

    # 2. Recursion guard.
    if payload.route_depth > 1:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="route_depth 超限，拒绝递归调用")

    if payload.conversation_id is not None:
        conversation = db.get(Conversation, payload.conversation_id)
        if conversation is None or str(conversation.user_id) != payload.user_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="会话上下文不可用")

    # 3. Execute via orchestrator (does NOT call Dify router workflow).
    context_dicts = [{"role": m.role, "content": m.content} for m in payload.context] if payload.context else None
    result = await orchestrator.run_callback(
        db,
        payload.query,
        context=context_dicts,
        conversation_id=payload.conversation_id,
        user_id=payload.user_id,
        route=payload.route,
        media_intent=payload.media_intent,
    )

    elapsed_ms = int((time.monotonic() - started) * 1000)
    # Structured log: request_id, route, elapsed, status only.
    logging.getLogger("business_ai.callback").info(
        "callback request_id=%s route=%s elapsed_ms=%d status=%s",
        payload.request_id,
        payload.route,
        elapsed_ms,
        "fallback" if result.used_fallback else "ok",
    )

    return LangGraphCallbackResponse(
        answer=result.answer,
        category=result.category,
        citations=result.citations,
        trace=result.trace,
        artifacts=result.artifacts,
        need_clarification=False,
        used_fallback=result.used_fallback,
    )


@router.post("/dify/customer-service", response_model=DifyWorkflowResponse, tags=["dify"])
async def run_dify_customer_service(
    payload: DifyWorkflowRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DifyWorkflowResponse:
    result = await dify_gateway.run_customer_service(payload.query, str(current_user.id))
    if result.degraded or not result.answer:
        local_result = await orchestrator.run(db, payload.query)
        detail = result.detail if result.degraded else "Dify 返回空回答，已执行本地回退"
        return DifyWorkflowResponse(
            answer=local_result.answer,
            mode="local_fallback",
            degraded=True,
            detail=detail,
            citations=local_result.citations,
            trace=[AgentTrace(step="Dify Gateway", status="fallback", detail=detail), *local_result.trace],
        )
    return DifyWorkflowResponse(
        answer=result.answer,
        mode=result.mode,
        degraded=result.degraded,
        detail=result.detail,
        citations=[],
        trace=[],
    )


def _dify_media_response(result) -> DifyMediaResponse:
    """Convert only verified remote media into a successful API response."""
    if (
        result.degraded
        or result.mode != "remote"
        or not result.output
        or not (result.media_url or result.data_url)
        or not result.content_type
    ):
        status_code = result.status_code if result.status_code in {502, 503} else 502
        raise HTTPException(status_code=status_code, detail=result.detail)
    return DifyMediaResponse(
        kind=result.kind,
        mode="remote",
        detail=result.detail,
        output=result.output,
        media_url=result.media_url,
        data_url=result.data_url,
        content_type=result.content_type,
        byte_size=result.byte_size,
    )


@router.post(
    "/dify/text-to-speech",
    response_model=DifyMediaResponse,
    tags=["dify"],
)
@router.post("/dify/tts", response_model=DifyMediaResponse, include_in_schema=False)
async def run_dify_text_to_speech(
    payload: DifyTextToSpeechRequest,
    current_user: User = Depends(get_current_user),
) -> DifyMediaResponse:
    if not payload.text.strip():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="文本不能为空")
    result = await dify_gateway.run_text_to_speech(payload.text, payload.voice, str(current_user.id))
    return _dify_media_response(result)


@router.post(
    "/dify/media/proxy",
    response_class=Response,
    tags=["dify"],
)
async def proxy_dify_media(
    payload: DifyMediaProxyRequest,
    _: User = Depends(get_current_user),
) -> Response:
    """Return verified provider bytes for browser media elements.

    The proxy is request-scoped and never stores or synthesizes media.  It is
    needed because an ``<audio>`` element cannot send this app's bearer token,
    and because the published TTS provider can emit non-canonical WAV sizes.
    """
    try:
        media = await dify_gateway.fetch_remote_media(payload.url, payload.kind)
    except DifyMediaProxyError as error:
        raise HTTPException(status_code=error.status_code, detail=error.detail) from error
    return Response(
        content=media.payload,
        media_type=media.content_type,
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post(
    "/dify/text-to-image",
    response_model=DifyMediaResponse,
    tags=["dify"],
)
@router.post("/dify/image", response_model=DifyMediaResponse, include_in_schema=False)
async def run_dify_text_to_image(
    payload: DifyTextToImageRequest,
    current_user: User = Depends(get_current_user),
) -> DifyMediaResponse:
    if not payload.prompt.strip():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="提示词不能为空")
    result = await dify_gateway.run_text_to_image(payload.prompt, payload.size, str(current_user.id))
    return _dify_media_response(result)
