"""Support tickets, handoff conversations, executive controls routes (mechanically split from app/api.py)."""

from . import shared
from .shared import *  # noqa: F401,F403

router = APIRouter()

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
        category = shared.workflow.classify(payload.question.strip())
    except (TypeError, ValueError, RuntimeError):
        category = "一般咨询"
    # A local/offline demo must remain deterministic and must not leave a
    # detached SQLite task holding a connection after the request.  When a
    # real model key is configured, the expensive draft is generated after
    # the response by ``_schedule_ticket_enrichment``.
    initial_reply = (
        TICKET_SUGGESTION_PENDING
        if shared.settings.llm_api_key
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
        quality_score=0.0 if shared.settings.llm_api_key else 0.74,
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    await shared.ticket_event_broker.publish(_ticket_event("created", ticket))
    # A fresh session prevents use-after-close of the request-scoped session;
    # the detached task lets the 201 response return before model generation.
    if shared.settings.llm_api_key:
        _schedule_ticket_enrichment(ticket.id, ticket.question, current_user.email)
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

        async with shared.ticket_event_broker.subscribe(support_ticket) as queue:
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

        async with shared.ticket_event_broker.subscribe(owned_ticket) as queue:
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
    await shared.ticket_event_broker.publish(_conversation_event("message", conversation, message))
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
    await shared.ticket_event_broker.publish(_conversation_event("assignment", conversation))
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
        await shared.ticket_event_broker.publish(_conversation_event("closed", conversation, message))
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
    await shared.ticket_event_broker.publish(_conversation_event("status", conversation))
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
    await shared.ticket_event_broker.publish(_conversation_event("read", conversation))
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

        async with shared.ticket_event_broker.subscribe(support_scope) as queue:
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
    await shared.ticket_event_broker.publish(_conversation_event("executive_takeover", conversation, message))
    await shared.ticket_event_broker.publish(_notification_event("created", notification, conversation=conversation))
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
    await shared.ticket_event_broker.publish(_notification_event("created", notification, conversation=conversation))
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
    await shared.ticket_event_broker.publish(_conversation_event("message", conversation, message))
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
    await shared.ticket_event_broker.publish(_notification_event("read", notification))
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

        async with shared.ticket_event_broker.subscribe(notification_scope) as queue:
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
    await shared.ticket_event_broker.publish(_ticket_event("updated", ticket))
    return ticket
