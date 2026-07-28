from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(80))
    role: Mapped[str] = mapped_column(String(32), default="enterprise_user", index=True)
    is_active: Mapped[bool] = mapped_column(default=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UserPreference(TimestampMixin, Base):
    __tablename__ = "user_preferences"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    response_style: Mapped[str] = mapped_column(String(20), default="balanced")
    preferred_language: Mapped[str] = mapped_column(String(10), default="zh-CN")
    auto_play_voice: Mapped[bool] = mapped_column(Boolean, default=False)


class KnowledgeDocument(TimestampMixin, Base):
    __tablename__ = "knowledge_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(255), index=True)
    source: Mapped[str] = mapped_column(String(255), default="手工录入")
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="ready")


class KnowledgeChunk(TimestampMixin, Base):
    __tablename__ = "knowledge_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("knowledge_documents.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    vector_json: Mapped[str] = mapped_column(Text)


class Conversation(TimestampMixin, Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(160), default="新会话")
    mode: Mapped[str] = mapped_column(String(32), default="assistant")
    # Keep the existing assistant/knowledge modes while making an AI-to-human
    # transition explicit and queryable.
    handoff_status: Mapped[str] = mapped_column(String(24), default="ai", index=True)
    assigned_agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    # An executive takeover is distinct from the support-agent assignment.
    # Keeping the controller explicit lets the API prevent a notified agent
    # from replying while the executive is handling the conversation.
    takeover_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    takeover_notice: Mapped[str | None] = mapped_column(Text, nullable=True)
    takeover_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    feedback_rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    feedback_helpful: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    feedback_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    feedback_submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Message(TimestampMixin, Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    trace_json: Mapped[str] = mapped_column(Text, default="[]")
    citations_json: Mapped[str] = mapped_column(Text, default="[]")
    artifacts_json: Mapped[str] = mapped_column(Text, default="[]")

    @property
    def artifacts(self) -> list[dict[str, object]]:
        """Decode persisted media metadata while keeping legacy rows readable."""
        try:
            parsed = json.loads(self.artifacts_json or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        if not isinstance(parsed, list):
            return []
        return [item for item in parsed if isinstance(item, dict)]

    # ``role`` is intentionally kept compatible with the agent/runtime
    # contract (``user``, ``assistant``, ``agent`` and ``system``).  These
    # read-only properties provide an explicit actor contract to clients so a
    # support-agent message can never be rendered as the enterprise user's
    # "me" bubble.
    @property
    def sender_role(self) -> str:
        return {
            "user": "enterprise_user",
            "enterprise_user": "enterprise_user",
            "assistant": "ai",
            "ai": "ai",
            "agent": "support_agent",
            "support_agent": "support_agent",
            "executive": "executive",
            "system": "system",
        }.get(self.role, "system")

    @property
    def sender_label(self) -> str:
        return {
            "enterprise_user": "企业用户",
            "ai": "AI",
            "executive": "经营管理者",
            "support_agent": "客服",
            "system": "系统",
        }.get(self.sender_role, "系统")

    @property
    def display_role(self) -> str:
        """Alias used by clients that prefer a display-oriented name."""
        return self.sender_label

    @property
    def actor_role(self) -> str:
        return self.sender_role

    @property
    def role_label(self) -> str:
        return self.sender_label

    @property
    def sender_type(self) -> str:
        return self.sender_role

    @property
    def sender_name(self) -> str:
        return self.sender_label


class SupportTicket(TimestampMixin, Base):
    __tablename__ = "support_tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Legacy seeded tickets may not have an owner; all newly-created tickets
    # are linked to the authenticated requester by the API.
    requester_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("conversations.id"), nullable=True, index=True
    )
    customer_name: Mapped[str] = mapped_column(String(80))
    question: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(64), index=True)
    priority: Mapped[str] = mapped_column(String(16), default="normal")
    status: Mapped[str] = mapped_column(String(24), default="open", index=True)
    suggested_reply: Mapped[str] = mapped_column(Text)
    final_reply: Mapped[str | None] = mapped_column(Text, nullable=True)
    quality_score: Mapped[float] = mapped_column(Float, default=0.0)


class AISetting(TimestampMixin, Base):
    __tablename__ = "ai_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    value: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(String(255), default="")


class SupportNotification(TimestampMixin, Base):
    """Durable, targeted notices sent to support agents by management.

    The in-process event broker delivers the notice immediately when an agent
    is connected; this row keeps it available after reconnecting or refreshing.
    """

    __tablename__ = "support_notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    recipient_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    sender_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("conversations.id"), nullable=True, index=True
    )
    content: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(32), default="executive_takeover", index=True)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def message(self) -> str:
        return self.content

    @property
    def agent_id(self) -> int:
        return self.recipient_id

    @property
    def notice(self) -> str:
        return self.content

    @property
    def sender_name(self) -> str | None:
        return None


class AdminAuditLog(Base):
    """Persisted trail of administrator operations for compliance audit."""

    __tablename__ = "admin_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    admin_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    admin_name: Mapped[str] = mapped_column(String(80), default="")
    action: Mapped[str] = mapped_column(String(64), index=True)
    target_type: Mapped[str] = mapped_column(String(32), default="")
    target_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_name: Mapped[str] = mapped_column(String(160), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    error_message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
