from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AISetting, KnowledgeDocument, SupportTicket, User
from ..security import hash_password
from .knowledge import index_document
from .runtime_settings import SETTING_DEFAULTS, SETTING_DESCRIPTIONS, ensure_runtime_settings


DEMO_PASSWORD = "Demo123!"


def seed_demo_data(db: Session) -> None:
    """Initialize deterministic data so a new checkout has a complete demo path."""
    if db.scalar(select(User.id).limit(1)) is not None:
        if ensure_runtime_settings(db):
            db.commit()
        return

    users = [
        User(
            email="enterprise@neusoft.local",
            password_hash=hash_password(DEMO_PASSWORD),
            display_name="陈晓明",
            role="enterprise_user",
        ),
        User(
            email="support@neusoft.local",
            password_hash=hash_password(DEMO_PASSWORD),
            display_name="林客服",
            role="support_agent",
        ),
        User(
            email="admin@neusoft.local",
            password_hash=hash_password(DEMO_PASSWORD),
            display_name="王管理员",
            role="admin",
        ),
        User(
            email="executive@neusoft.local",
            password_hash=hash_password(DEMO_PASSWORD),
            display_name="赵经理",
            role="executive",
        ),
    ]
    knowledge = [
        KnowledgeDocument(
            title="商务服务响应与升级规范",
            source="客服中心制度 V2.1",
            content=(
                "普通商务咨询应在 2 个工作小时内首次响应，并在 1 个工作日内给出处理结论。"
                "涉及合同、付款、发票的事项由商务专员复核；涉及系统故障的事项先登记工单，再由技术支持在 30 分钟内确认。"
                "紧急事项包括服务中断、资金风险、数据安全事件，必须升级为 urgent 并通知值班负责人。"
            ),
        ),
        KnowledgeDocument(
            title="合同与发票办理指引",
            source="商务运营手册",
            content=(
                "合同审批需提交合同版本、客户主体信息、报价单和审批单。标准合同在材料齐全后 2 个工作日内完成初审。"
                "开票申请需要提供订单号、开票抬头、税号、金额与邮箱；已完成交付确认的订单可在 1 个工作日内安排开票。"
                "涉及合同条款变更时，客服不得承诺具体法律结论，应转交商务与法务联合审核。"
            ),
        ),
        KnowledgeDocument(
            title="企业账户与订单查询说明",
            source="平台操作指南",
            content=(
                "企业管理员可在订单中心查看订单状态、服务周期和交付记录。忘记密码时可通过注册邮箱重置；若邮箱不可用，请提交企业认证材料。"
                "订单状态包括待确认、履约中、待验收、已完成和已关闭。客户可通过订单号查询进度，客服只展示当前授权范围内的信息。"
            ),
        ),
        KnowledgeDocument(
            title="AI 助手回答边界与质检规则",
            source="AI 服务运行规范",
            content=(
                "AI 助手应基于已检索的企业知识生成答案，无法确认时需明确说明并建议转人工。"
                "不得编造订单状态、价格、合同条款或个人信息。回复需包含下一步建议，涉及紧急风险时应提示立即转人工。"
                "客服回复完成后由质检规则检查敏感承诺、答复完整性与知识依据。"
            ),
        ),
    ]
    tickets = [
        SupportTicket(
            customer_name="星河科技",
            question="我们上周提交的开票申请还没有收到邮件，想确认处理进度。",
            category="发票办理",
            priority="normal",
            status="open",
            suggested_reply="您好，已为您登记开票进度核查。请补充订单号和开票抬头，我们会在 1 个工作日内反馈处理结果。",
            quality_score=0.94,
        ),
        SupportTicket(
            customer_name="华南物流",
            question="客户账号无法登录，重置邮件没有收到。",
            category="账户访问",
            priority="high",
            status="in_progress",
            suggested_reply="您好，请先确认注册邮箱的垃圾邮件箱；若仍未收到，请提供企业名称和注册邮箱，我们将协助核验并升级处理。",
            quality_score=0.91,
        ),
        SupportTicket(
            customer_name="启明制造",
            question="服务突然不可用，影响今天的客户演示。",
            category="系统故障",
            priority="urgent",
            status="open",
            suggested_reply="抱歉影响了您的使用。该问题已按紧急故障升级，请提供服务地址和发生时间，技术支持会在 30 分钟内确认。",
            quality_score=0.97,
        ),
    ]
    ai_settings = [
        AISetting(key=key, value=value, description=SETTING_DESCRIPTIONS[key])
        for key, value in SETTING_DEFAULTS.items()
    ]
    db.add_all([*users, *knowledge])
    db.flush()
    for document in knowledge:
        index_document(db, document)
    db.add_all([*tickets, *ai_settings])
    db.commit()
    if ensure_runtime_settings(db):
        db.commit()
