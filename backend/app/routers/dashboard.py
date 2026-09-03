"""Operational dashboards and reports routes (mechanically split from app/api.py)."""

from . import shared
from .shared import *  # noqa: F401,F403

router = APIRouter()

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
            "provider": "OpenAI compatible 已配置" if shared.settings.llm_api_key else "本地演示模型",
            "dify": "已配置，调用时检查" if shared.settings.dify_api_url and shared.settings.dify_api_key else "未配置，按请求本地回退",
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
