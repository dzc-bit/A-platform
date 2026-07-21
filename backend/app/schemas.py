from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


Role = Literal["enterprise_user", "support_agent", "admin", "executive"]


class APIModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class UserOut(APIModel):
    id: int
    email: str
    display_name: str
    role: Role
    is_active: bool
    created_at: datetime


class UserPreferenceUpdate(BaseModel):
    response_style: Literal["concise", "balanced", "detailed"] = "balanced"
    preferred_language: Literal["zh-CN", "en-US"] = "zh-CN"
    auto_play_voice: bool = False


class UserPreferenceOut(APIModel):
    response_style: Literal["concise", "balanced", "detailed"]
    preferred_language: Literal["zh-CN", "en-US"]
    auto_play_voice: bool


class RegisterRequest(BaseModel):
    email: str = Field(min_length=5, max_length=255)
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(min_length=2, max_length=80)


class LoginRequest(BaseModel):
    email: str
    password: str


class AuthResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    user: UserOut


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: int | None = None
    mode: Literal["assistant", "knowledge"] = "assistant"


class Citation(BaseModel):
    document_id: int
    title: str
    excerpt: str
    score: float


class AgentTrace(BaseModel):
    step: str
    status: Literal["completed", "skipped", "fallback"]
    detail: str


class ChatResponse(BaseModel):
    conversation_id: int
    answer: str
    citations: list[Citation]
    trace: list[AgentTrace]
    used_fallback: bool
    # A normal AI answer can offer a deliberate handoff without changing the
    # existing answer/citation contract.  The client decides when to invoke
    # the explicit handoff endpoint.
    handoff_available: bool = False
    handoff_requested: bool = False


class ConversationFeedbackRequest(BaseModel):
    rating: int = Field(ge=1, le=5)
    helpful: bool
    comment: str | None = Field(default=None, max_length=1000)


class ConversationFeedbackOut(BaseModel):
    rating: int
    helpful: bool
    comment: str | None = None
    submitted_at: datetime


class ImageAnalysisResponse(BaseModel):
    answer: str
    used_fallback: bool
    detail: str


class ConversationOut(APIModel):
    id: int
    title: str
    mode: str
    handoff_status: str = "ai"
    assigned_agent_id: int | None = None
    takeover_by_id: int | None = None
    takeover_by: dict[str, Any] | None = None
    takeover_notice: str | None = None
    takeover_at: datetime | None = None
    control_mode: str = "support_agent"
    last_notification: "SupportNotificationOut | None" = None
    updated_at: datetime
    # The fields below are populated for the support queue.  They remain
    # optional/defaulted so the enterprise conversation list keeps its small
    # existing payload while the same contract can represent a rich queue
    # row without introducing a second, incompatible conversation resource.
    user_id: int | None = None
    customer_id: int | None = None
    customer_name: str | None = None
    customer_email: str | None = None
    customer_display_name: str | None = None
    user: dict[str, Any] | None = None
    customer: dict[str, Any] | None = None
    assigned_agent: dict[str, Any] | None = None
    status: str | None = None
    unread_count: int = Field(default=0, ge=0)
    priority: str = "normal"
    related_ticket_id: int | None = None
    ticket_id: int | None = None
    related_ticket: dict[str, Any] | None = None
    recent_message: "MessageOut | None" = None
    last_message: "MessageOut | None" = None
    feedback_rating: int | None = Field(default=None, ge=1, le=5)
    feedback_helpful: bool | None = None
    feedback_comment: str | None = None
    feedback_submitted_at: datetime | None = None


class MessageOut(APIModel):
    id: int
    conversation_id: int
    role: str
    content: str
    created_at: datetime
    # Keep ``role`` for backwards compatibility with the orchestration and
    # existing clients.  New clients should render the explicit actor fields.
    sender_role: str = "system"
    sender_label: str = "系统"
    display_role: str = "系统"
    actor_role: str = "system"
    role_label: str = "系统"
    sender_type: str = "system"
    sender_name: str | None = None


class AuditMessageOut(MessageOut):
    """A message with the persisted AI audit payloads attached."""

    trace: list[AgentTrace] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)


class ConversationAuditSummary(APIModel):
    """A compact, ownership-independent conversation row for audit screens."""

    id: int
    title: str
    mode: str
    handoff_status: str = "ai"
    status: str | None = None
    updated_at: datetime
    user_id: int | None = None
    customer_name: str | None = None
    customer_email: str | None = None
    user: dict[str, Any] | None = None
    customer: dict[str, Any] | None = None
    assigned_agent_id: int | None = None
    assigned_agent: dict[str, Any] | None = None
    takeover_by_id: int | None = None
    takeover_notice: str | None = None
    takeover_at: datetime | None = None
    control_mode: str = "support_agent"
    message_count: int = Field(default=0, ge=0)
    recent_message: AuditMessageOut | None = None
    feedback_rating: int | None = Field(default=None, ge=1, le=5)
    feedback_helpful: bool | None = None
    feedback_comment: str | None = None
    feedback_submitted_at: datetime | None = None


class ConversationAuditDetail(ConversationAuditSummary):
    """Full read-only transcript used by administrator/executive viewers."""

    messages: list[AuditMessageOut] = Field(default_factory=list)


DashboardDetailScope = Literal[
    "tickets",
    "status",
    "category",
    "consultations",
    "satisfaction",
    "insights",
]


class DashboardDetailsOut(BaseModel):
    scope: DashboardDetailScope
    title: str
    summary: str
    rows: list[dict[str, Any]] = Field(default_factory=list)


class ConversationMessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=4000)


class SupportAssistantRequest(BaseModel):
    """Prompt for the support-only drafting assistant.

    Knowledge is an optional reference rather than a hard grounding contract;
    the endpoint is intentionally separate from the enterprise assistant.
    """

    query: str = Field(
        min_length=1,
        max_length=4000,
        validation_alias=AliasChoices("query", "message"),
    )
    conversation_id: int | None = Field(default=None, ge=1)
    # The support copilot is a general drafting model by default; agents can
    # opt into knowledge references for a particular request.
    use_knowledge: bool = False


class SupportAssistantResponse(BaseModel):
    answer: str
    citations: list[Citation] = Field(default_factory=list)
    trace: list[AgentTrace] = Field(default_factory=list)
    used_fallback: bool = False
    model_mode: str = "support_hybrid"
    model: str | None = None
    knowledge_used: bool = False
    category: str = ""


class ExecutiveTakeoverRequest(BaseModel):
    assigned_agent_id: int = Field(ge=1)
    notice: str | None = Field(default=None, max_length=2000)


class ExecutiveNotificationRequest(BaseModel):
    """Notification payload with aliases used by older support clients."""

    assigned_agent_id: int | None = Field(default=None, ge=1)
    agent_id: int | None = Field(default=None, ge=1)
    notice: str | None = Field(default=None, max_length=2000)
    message: str | None = Field(default=None, max_length=2000)

    def resolved_agent_id(self) -> int | None:
        return self.assigned_agent_id or self.agent_id

    def resolved_message(self) -> str | None:
        return self.notice or self.message


class SupportNotificationOut(APIModel):
    id: int
    recipient_id: int
    sender_id: int
    conversation_id: int | None = None
    content: str
    # Compatibility aliases make the targeted SSE payload easy to consume by
    # both the current and earlier support workspaces.
    message: str | None = None
    agent_id: int | None = None
    notice: str | None = None
    sender_name: str | None = None
    kind: str
    is_read: bool
    read_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ConversationAssignmentUpdate(BaseModel):
    """Assign a handoff conversation to one support agent, or unassign it."""

    assigned_agent_id: int | None = None


class ConversationStatusUpdate(BaseModel):
    status: Literal["requested", "active", "closed"]


class HandoffResponse(BaseModel):
    conversation_id: int
    status: Literal["requested", "active", "closed"]
    message: MessageOut


class KnowledgeCreate(BaseModel):
    title: str = Field(min_length=2, max_length=255)
    source: str = Field(default="手工录入", max_length=255)
    content: str = Field(min_length=20, max_length=100000)


class KnowledgeOut(APIModel):
    id: int
    title: str
    source: str
    content: str
    status: str
    created_at: datetime
    updated_at: datetime


class KnowledgeReindexOut(BaseModel):
    document: KnowledgeOut
    status: str
    indexed_chunks: int


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    top_k: int = Field(default=3, ge=1, le=8)


class SearchResponse(BaseModel):
    results: list[Citation]


class TicketCreate(BaseModel):
    customer_name: str = Field(min_length=2, max_length=80)
    question: str = Field(min_length=5, max_length=4000)
    priority: Literal["low", "normal", "high", "urgent"] = "normal"
    conversation_id: int | None = Field(default=None, ge=1)


class TicketUpdate(BaseModel):
    status: Literal["open", "in_progress", "resolved"] | None = None
    final_reply: str | None = Field(default=None, max_length=4000)


class TicketOut(APIModel):
    id: int
    requester_id: int | None = None
    conversation_id: int | None = None
    customer_name: str
    question: str
    category: str
    priority: str
    status: str
    suggested_reply: str
    final_reply: str | None
    quality_score: float
    created_at: datetime
    updated_at: datetime


class SettingOut(APIModel):
    id: int
    key: str
    value: str
    description: str
    updated_at: datetime


class SettingUpdate(BaseModel):
    value: str = Field(max_length=10000)
    description: str = Field(default="", max_length=255)


class UserRoleUpdate(BaseModel):
    role: Role
    is_active: bool


class DifyWorkflowRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)


class DifyWorkflowResponse(BaseModel):
    answer: str
    mode: Literal["remote", "local_fallback"]
    degraded: bool
    detail: str
    citations: list[Citation] = Field(default_factory=list)
    trace: list[AgentTrace] = Field(default_factory=list)


class DifyTextToSpeechRequest(BaseModel):
    text: str = Field(min_length=1, max_length=6000)
    voice: str = Field(default="default", max_length=40)


class DifyTextToImageRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=2000)
    size: Literal[
        "1024x1024",
        "1280x720",
        "720x1280",
        "2048*2048",
        "2688*1536",
        "1536*2688",
    ] = "1024x1024"


class DifyMediaProxyRequest(BaseModel):
    """Request body for the authenticated, short-lived media proxy."""

    url: str = Field(min_length=1, max_length=12_000)
    kind: Literal["audio", "image"] = "audio"


class DifyMediaResponse(BaseModel):
    """Only a verified external media reference can produce this response."""

    kind: Literal["audio", "image"]
    mode: Literal["remote"]
    degraded: Literal[False] = False
    detail: str
    output: dict[str, Any]
    media_url: str | None = None
    data_url: str | None = None
    content_type: str
    byte_size: int | None = Field(default=None, ge=1)


# ``ConversationOut`` refers to ``MessageOut`` because the conversation
# contract is declared before the message contract for readability.  Resolve
# that forward reference once all schemas have been declared.
ConversationOut.model_rebuild()
