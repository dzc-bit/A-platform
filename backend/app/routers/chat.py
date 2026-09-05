"""Assistant conversations, chat streaming and image analysis routes (mechanically split from app/api.py)."""

from . import shared
from .shared import (  # noqa: F401
    APIRouter,
    AgentStreamCompleted,
    AgentStreamReset,
    AgentStreamToken,
    AgentStreamTrace,
    AgentTrace,
    ChatRequest,
    ChatResponse,
    Conversation,
    ConversationFeedbackOut,
    ConversationFeedbackRequest,
    ConversationMessageCreate,
    ConversationOut,
    Depends,
    File,
    Form,
    HTTPException,
    HandoffResponse,
    ImageAnalysisResponse,
    MAX_IMAGE_UPLOAD_BYTES,
    Message,
    MessageOut,
    Request,
    Session,
    StreamingResponse,
    SupportAssistantRequest,
    SupportAssistantResponse,
    SupportTicket,
    UploadFile,
    User,
    _begin_chat,
    _cache_hit_result,
    _conversation_event,
    _conversation_message,
    _image_media_type,
    _owned_conversation,
    _persist_chat_result,
    _public_conversation_event,
    _run_chat,
    _support_conversation,
    asyncio,
    build_answer_cache_key,
    datetime,
    delete,
    deserialize_agent_result,
    final_answer_cache,
    get_current_user,
    get_db,
    get_runtime_settings,
    get_user_preference,
    json,
    preference_instruction,
    require_roles,
    select,
    serialize_agent_result,
    should_cache_result,
    status,
    timezone,
    update,
)

router = APIRouter()

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
    await shared.ticket_event_broker.publish(_conversation_event("handoff", conversation, message))
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
    await shared.ticket_event_broker.publish(_conversation_event("message", conversation, message))
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

        async with shared.ticket_event_broker.subscribe(owned) as queue:
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
    result = await shared.workflow.run_support_assistant(
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
            if shared.settings.dify_router_api_key and shared.settings.dify_api_url:
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
                    await shared.ticket_event_broker.publish(_conversation_event("message", conversation, assistant_message))
                yield f"event: done\ndata: {result.model_dump_json()}\n\n"
                return
            async for event in shared.workflow.stream(
                db,
                payload.message.strip(),
                conversation_id=conversation.id,
                preference_instruction=preference_instruction(preference),
                user_email=current_user.email,
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
                        await shared.ticket_event_broker.publish(_conversation_event("message", conversation, assistant_message))
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
    result = await shared.vision_service.analyze(
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
