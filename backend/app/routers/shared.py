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

from ..config import DEMO_TOKEN_SECRETS, settings
from ..database import SessionLocal, get_db
from ..dependencies import get_current_user, require_roles
from ..models import (
    AdminAuditLog,
    AISetting,
    Conversation,
    KnowledgeDocument,
    Message,
    SupportNotification,
    SupportTicket,
    User,
)
from ..schemas import (
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
from ..security import create_access_token, hash_password, verify_password
from ..services.agent import (
    AgentResult,
    AgentStreamCompleted,
    AgentStreamReset,
    AgentStreamToken,
    AgentStreamTrace,
    AssistantWorkflow,
    LANGGRAPH_AVAILABLE,
)
from ..services.answer_cache import (
    build_answer_cache_key,
    deserialize_agent_result,
    final_answer_cache,
    serialize_agent_result,
    should_cache_result,
)
from ..services.cache import retrieval_cache
from ..services.dify import DifyGateway, DifyMediaProxyError
from ..services.events import ticket_event_broker
from ..services.knowledge import index_document, remove_document, retrieve
from ..services.preferences import get_user_preference, preference_instruction
from ..services.runtime_settings import get_runtime_settings, validate_setting, SETTING_DEFAULTS, SETTING_DESCRIPTIONS
from ..services.admin_audit import record_admin_action
from ..services.vision import VisionService

workflow = AssistantWorkflow()
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


async def _enrich_ticket_suggestion(ticket_id: int, question: str, owner_email: str | None = None) -> None:
    """Generate the expensive AI draft after the ticket response is sent.

    ``owner_email`` scopes order lookups inside the workflow to the ticket
    requester; without it those lookups fail closed.
    """
    try:
        with SessionLocal() as task_db:
            result = await workflow.run(task_db, question, user_email=owner_email)
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
            ticket.quality_score = round(result.quality_score, 2) if result.quality_score else 0.74
            task_db.commit()
            task_db.refresh(ticket)
            event = _ticket_event("updated", ticket)
        # Publish unconditionally: with the Redis broker, has_subscribers() only
        # sees THIS process's SSE consumers, so gating here would silently drop
        # the event for consumers attached to other workers. Publishing to a
        # broker without subscribers is a no-op.
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
            await ticket_event_broker.publish(event)
        except Exception:
            # Background errors are intentionally isolated from the request
            # lifecycle; the ticket remains visible for manual handling.
            return


def _schedule_ticket_enrichment(ticket_id: int, question: str, owner_email: str | None = None) -> None:
    """Detach enrichment from the request while retaining task references."""
    task = asyncio.create_task(_enrich_ticket_suggestion(ticket_id, question, owner_email))
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
    """Try Dify router workflow first; fall back to local LangGraph workflow."""
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
    # Fallback: local LangGraph workflow.
    return await workflow.run(
        db,
        payload.message.strip(),
        conversation_id=conversation.id,
        preference_instruction=preference_instruction(preference),
        user_email=user.email,
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

__all__ = [
    "AISetting",
    "APIRouter",
    "AdminAuditLog",
    "AdminAuditLogOut",
    "AdminAuditLogPage",
    "AgentStreamCompleted",
    "AgentStreamReset",
    "AgentStreamToken",
    "AgentStreamTrace",
    "AgentTrace",
    "AuthResponse",
    "ChatRequest",
    "ChatResponse",
    "Conversation",
    "ConversationAssignmentUpdate",
    "ConversationAuditDetail",
    "ConversationAuditSummary",
    "ConversationFeedbackOut",
    "ConversationFeedbackRequest",
    "ConversationMessageCreate",
    "ConversationOut",
    "ConversationStatusUpdate",
    "DashboardDetailScope",
    "DashboardDetailsOut",
    "Depends",
    "DifyMediaProxyError",
    "DifyMediaProxyRequest",
    "DifyMediaResponse",
    "DifyTextToImageRequest",
    "DifyTextToSpeechRequest",
    "DifyWorkflowRequest",
    "DifyWorkflowResponse",
    "ElementTree",
    "ExecutiveNotificationRequest",
    "ExecutiveTakeoverRequest",
    "File",
    "Form",
    "HTTPException",
    "HandoffResponse",
    "Header",
    "ImageAnalysisResponse",
    "KnowledgeCreate",
    "KnowledgeDocument",
    "KnowledgeOut",
    "KnowledgeReindexOut",
    "LANGGRAPH_AVAILABLE",
    "LangGraphCallbackRequest",
    "LangGraphCallbackResponse",
    "LoginRequest",
    "MAX_CSV_COLUMNS",
    "MAX_CSV_ROWS",
    "MAX_DOCX_COMPRESSION_RATIO",
    "MAX_DOCX_MEMBERS",
    "MAX_DOCX_UNCOMPRESSED_BYTES",
    "MAX_EXTRACTED_TEXT_CHARS",
    "MAX_IMAGE_UPLOAD_BYTES",
    "MAX_PDF_PAGES",
    "MAX_UPLOAD_BYTES",
    "Message",
    "MessageOut",
    "PdfReadError",
    "PdfReader",
    "PurePosixPath",
    "Query",
    "RegisterRequest",
    "Request",
    "Response",
    "SETTING_DEFAULTS",
    "SETTING_DESCRIPTIONS",
    "SUPPORTED_DOCUMENT_SUFFIXES",
    "SearchRequest",
    "SearchResponse",
    "Session",
    "SettingOut",
    "SettingUpdate",
    "StreamingResponse",
    "SupportAssistantRequest",
    "SupportAssistantResponse",
    "SupportNotification",
    "SupportNotificationOut",
    "SupportTicket",
    "TICKET_SUGGESTION_PENDING",
    "TicketCreate",
    "TicketOut",
    "TicketUpdate",
    "UploadFile",
    "User",
    "UserCreate",
    "UserOut",
    "UserPreferenceOut",
    "UserPreferenceUpdate",
    "UserResetPassword",
    "UserRoleUpdate",
    "_TICKET_TRANSITIONS",
    "_audit_message_payload",
    "_auth_response",
    "_begin_chat",
    "_cache_hit_result",
    "_conversation_audit_payload",
    "_conversation_audit_summary",
    "_conversation_event",
    "_conversation_message",
    "_conversation_response",
    "_ensure_support_control_allowed",
    "_executive_conversation",
    "_image_media_type",
    "_notification_event",
    "_owned_conversation",
    "_persist_chat_result",
    "_public_conversation_event",
    "_public_support_conversation_event",
    "_public_ticket_event",
    "_run_chat",
    "_schedule_ticket_enrichment",
    "_support_conversation",
    "_ticket_event",
    "_token_secret_security",
    "asyncio",
    "build_answer_cache_key",
    "csv",
    "datetime",
    "delete",
    "deserialize_agent_result",
    "final_answer_cache",
    "func",
    "get_current_user",
    "get_db",
    "get_runtime_settings",
    "get_user_preference",
    "hash_password",
    "index_document",
    "io",
    "json",
    "logging",
    "preference_instruction",
    "re",
    "record_admin_action",
    "remove_document",
    "require_roles",
    "retrieval_cache",
    "retrieve",
    "secrets",
    "select",
    "serialize_agent_result",
    "should_cache_result",
    "status",
    "time",
    "timedelta",
    "timezone",
    "update",
    "validate_setting",
    "verify_password",
    "zipfile",
]
