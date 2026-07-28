from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field as dataclass_field
from typing import Any, Literal, TypedDict

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Message, SupportTicket
from ..schemas import AgentTrace, Artifact, Citation
from .knowledge import RetrievalHit, retrieve
from .dify import DifyGateway
from .llm import (
    Completion,
    LLMHistoryMessage,
    LLMStreamCompleted,
    LLMStreamTextDelta,
    LLMStreamToolCallDelta,
    LLMToolCall,
    LLMToolDefinition,
    LLMToolResult,
    OpenAICompatibleClient,
)
from .runtime_settings import (
    DEFAULT_ASSISTANT_PROMPT,
    RuntimeSettings,
    get_runtime_settings,
    runtime_prompt_instruction,
)

try:  # Optional production integration. The local state graph is the offline fallback.
    from langgraph.graph import END, StateGraph  # type: ignore

    LANGGRAPH_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on optional deployment package
    END = StateGraph = None  # type: ignore
    LANGGRAPH_AVAILABLE = False

try:
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_core.runnables import (
        RunnableBranch,
        RunnableLambda,
        RunnableParallel,
        RunnablePassthrough,
    )

    LCEL_AVAILABLE = True
except ImportError:  # pragma: no cover - preserves the offline fallback before optional installs
    ChatPromptTemplate = RunnableBranch = RunnableLambda = RunnableParallel = RunnablePassthrough = None  # type: ignore[assignment,misc]
    LCEL_AVAILABLE = False


_SUMMARY_SOURCE_MESSAGES = 40
_SUMMARY_MAX_CHARS = 600


@dataclass(frozen=True)
class AgentResult:
    answer: str
    citations: list[Citation]
    trace: list[AgentTrace]
    used_fallback: bool
    category: str
    artifacts: list[Artifact] = dataclass_field(default_factory=list)


@dataclass(frozen=True)
class AgentStreamTrace:
    trace: AgentTrace


@dataclass(frozen=True)
class AgentStreamToken:
    text: str
    origin: Literal["model", "fallback"]


@dataclass(frozen=True)
class AgentStreamReset:
    text: str


@dataclass(frozen=True)
class AgentStreamCompleted:
    result: AgentResult


class AgentWorkflowState(TypedDict, total=False):
    question: str
    top_k: int
    cache_ttl_seconds: int
    assistant_prompt: str
    category: str
    hits: list[RetrievalHit]
    tasks: list[str]
    tool_name: str | None
    tool_arguments: dict[str, object]
    response_plan: dict[str, str]
    plan_attempts: int
    plan_valid: bool


class ClassificationAgent:
    """Dedicated intent agent backed by a small deterministic LCEL runnable."""

    def __init__(self, classifier: Any) -> None:
        self._classifier = classifier
        self._chain = (
            RunnableLambda(lambda state: {"category": classifier(state["question"])}) if LCEL_AVAILABLE else None
        )

    def run(self, question: str) -> str:
        if self._chain is not None:
            return str(self._chain.invoke({"question": question})["category"])
        return str(self._classifier(question))


class KnowledgeQueryAgent:
    """Retrieve grounded knowledge independently from the response-generation agent."""

    def __init__(self) -> None:
        self._chain = (
            RunnableLambda(
                lambda state: {
                    "hits": retrieve(
                        state["db"],
                        state["question"],
                        top_k=state["top_k"],
                        cache_ttl_seconds=state["cache_ttl_seconds"],
                    )
                }
            )
            if LCEL_AVAILABLE
            else None
        )

    def run(self, db: Session, question: str, top_k: int, cache_ttl_seconds: int) -> list[RetrievalHit]:
        if self._chain is not None:
            return list(
                self._chain.invoke(
                    {
                        "db": db,
                        "question": question,
                        "top_k": top_k,
                        "cache_ttl_seconds": cache_ttl_seconds,
                    }
                )["hits"]
            )
        return retrieve(db, question, top_k=top_k, cache_ttl_seconds=cache_ttl_seconds)


class ResponseAgent:
    """Prepare a grounded response prompt through an LCEL prompt pipeline."""

    def __init__(self) -> None:
        self._chain: Any | None = None
        self._parallel: Any | None = None
        self._branch: Any | None = None
        if (
            LCEL_AVAILABLE
            and ChatPromptTemplate is not None
            and RunnableBranch is not None
            and RunnableParallel is not None
            and RunnablePassthrough is not None
        ):
            grounded_prompt = ChatPromptTemplate.from_messages(
                [
                    ("system", "{assistant_prompt}"),
                    (
                        "human",
                        "用户问题：{question}\n意图：{category}\n企业知识：{context}\n业务工具结果：{tool_result}",
                    ),
                ]
            )
            cautious_prompt = ChatPromptTemplate.from_messages(
                [
                    ("system", "{assistant_prompt}"),
                    (
                        "human",
                        "用户问题：{question}\n意图：{category}\n企业知识：无\n"
                        "回答策略：未检索到可用来源，只能说明限制并建议转人工核验。\n"
                        "业务工具结果：{tool_result}",
                    ),
                ]
            )
            self._parallel = RunnableParallel(
                state=RunnablePassthrough(),
                context=RunnableLambda(
                    lambda state: "\n".join(
                        f"[{item.title}] {item.excerpt}" for item in state.get("hits", [])
                    )
                    or "无"
                ),
                tool_result=RunnableLambda(lambda state: state.get("tool_result") or "无"),
            )
            self._branch = RunnableBranch(
                (lambda state: bool(state.get("hits")), grounded_prompt),
                cautious_prompt,
            )
            self._chain = (
                self._parallel
                | RunnableLambda(
                    lambda values: {
                        **values["state"],
                        "context": values["context"],
                        "tool_result": values["tool_result"],
                    }
                )
                | self._branch
                | RunnableLambda(self._messages_to_payload)
            )

    @staticmethod
    def _messages_to_payload(prompt_value: Any) -> dict[str, str]:
        messages = prompt_value.to_messages()
        system_prompt = next((str(message.content) for message in messages if message.type == "system"), "")
        user_prompt = next((str(message.content) for message in messages if message.type == "human"), "")
        return {"system_prompt": system_prompt, "user_prompt": user_prompt}

    def prepare(
        self,
        *,
        assistant_prompt: str,
        question: str,
        category: str,
        hits: list[RetrievalHit],
        tool_result: str | None,
    ) -> dict[str, str]:
        state = {
            "assistant_prompt": assistant_prompt,
            "question": question,
            "category": category,
            "hits": hits,
            "tool_result": tool_result,
        }
        if self._chain is not None:
            return dict(self._chain.invoke(state))
        context = "\n".join(f"[{item.title}] {item.excerpt}" for item in hits) or "无"
        evidence_note = "" if hits else "\n回答策略：未检索到可用来源，只能说明限制并建议转人工核验。"
        return {
            "system_prompt": assistant_prompt,
            "user_prompt": (
                f"用户问题：{question}\n意图：{category}\n企业知识：{context}{evidence_note}"
                f"\n业务工具结果：{tool_result or '无'}"
            ),
        }


class QualityAgent:
    """Run the deterministic answer-quality gate as its own LCEL agent."""

    def __init__(self, checker: Any) -> None:
        self._checker = checker
        self._chain = (
            RunnableLambda(lambda state: checker(state["answer"], state["has_sources"])) if LCEL_AVAILABLE else None
        )

    def run(self, answer: str, has_sources: bool) -> tuple[str, str]:
        if self._chain is not None:
            status, detail = self._chain.invoke({"answer": answer, "has_sources": has_sources})
            return str(status), str(detail)
        status, detail = self._checker(answer, has_sources)
        return str(status), str(detail)


class EmailDraftAgent:
    """Create a reviewable escalation draft only; this agent never sends messages."""

    def __init__(self) -> None:
        self._chain = RunnableLambda(self._draft) if LCEL_AVAILABLE else None

    @staticmethod
    def _draft(state: dict[str, str]) -> str | None:
        category = state["category"]
        if category not in {"系统故障", "付款咨询", "合同咨询"}:
            return None
        return (
            f"主题：请人工复核 - {category}\n"
            f"正文：收到用户咨询“{state['question'][:120]}”。"
            "该草稿仅供客服确认，不会由系统自动发送；请核验业务事实、收件人和附件后再处理。"
        )

    def run(self, category: str, question: str) -> str | None:
        state = {"category": category, "question": question}
        if self._chain is not None:
            result = self._chain.invoke(state)
            return str(result) if result else None
        return self._draft(state)


class BusinessAgentOrchestrator:
    """Classification -> retrieval/tool -> answer -> quality check orchestration.

    The same state transitions map directly to a LangGraph StateGraph when the optional
    dependency is installed. Keeping the fallback explicit makes classroom demos and
    tests deterministic while preserving a production migration path.
    """

    def __init__(self, llm_client: OpenAICompatibleClient | None = None, dify_gateway: DifyGateway | None = None) -> None:
        self.llm_client = llm_client or OpenAICompatibleClient()
        self.dify_gateway = dify_gateway or DifyGateway()
        self.classification_agent = ClassificationAgent(self.classify)
        self.knowledge_query_agent = KnowledgeQueryAgent()
        self.response_agent = ResponseAgent()
        self.quality_agent = QualityAgent(self._quality_check)
        self.email_draft_agent = EmailDraftAgent()

    _TOOL_NAMES_BY_CATEGORY = {
        "工单统计": "support_ticket_queue_summary",
        "系统故障": "system_incident_escalation",
        "订单查询": "order_query_privacy_notice",
        "付款咨询": "payment_manual_review",
        "合同咨询": "contract_review_handoff",
        "语音生成": "dify_text_to_speech",
        "图片生成": "dify_text_to_image",
    }
    _TOOL_RESULTS = {
        "system_incident_escalation": (
            "已触发故障升级工具：请收集服务地址、发生时间和影响范围，技术支持会在 30 分钟内确认。"
        ),
        "order_query_privacy_notice": (
            "订单查询工具需要订单号；演示环境不会返回真实订单数据，以保护客户信息。"
        ),
        "payment_manual_review": (
            "付款相关问题已标记为需商务专员复核，系统不会展示或推测真实资金信息。"
        ),
        "contract_review_handoff": (
            "合同咨询工具已提示转交商务与法务联合审核，客服不会承诺具体条款结论。"
        ),
    }
    _TOOL_DEFINITIONS = {
        "support_ticket_queue_summary": LLMToolDefinition(
            name="support_ticket_queue_summary",
            description=(
                "Count support tickets in one allowed status. Returns aggregates only and never customer rows."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["open", "in_progress", "resolved"],
                    }
                },
                "required": ["status"],
                "additionalProperties": False,
            },
        ),
        "system_incident_escalation": LLMToolDefinition(
            name="system_incident_escalation",
            description="Return a deterministic incident escalation checklist. It never reads customer data.",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
        ),
        "order_query_privacy_notice": LLMToolDefinition(
            name="order_query_privacy_notice",
            description="Return the safe order-query handoff guidance without looking up any order.",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
        ),
        "payment_manual_review": LLMToolDefinition(
            name="payment_manual_review",
            description="Return the deterministic payment-review handoff guidance without financial data.",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
        ),
        "contract_review_handoff": LLMToolDefinition(
            name="contract_review_handoff",
            description="Return the deterministic contract-review handoff guidance without contract data.",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
        ),
        "dify_text_to_speech": LLMToolDefinition(
            name="dify_text_to_speech",
            description="Convert text to speech audio via Dify TTS workflow. Returns a playable audio reference.",
            parameters={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The text to convert to speech (1-6000 chars).",
                    },
                    "voice": {
                        "type": "string",
                        "description": "Voice style identifier. Defaults to 'Cherry'.",
                    },
                },
                "required": ["text"],
                "additionalProperties": False,
            },
        ),
        "dify_text_to_image": LLMToolDefinition(
            name="dify_text_to_image",
            description="Generate an image from a text prompt via Dify image workflow. Returns an image reference.",
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Description of the image to generate (1-2000 chars).",
                    },
                    "size": {
                        "type": "string",
                        "enum": [
                            "1024x1024",
                            "1280x720",
                            "720x1280",
                            "2048*2048",
                            "2688*1536",
                            "1536*2688",
                        ],
                        "description": "Output image size. Defaults to '1024x1024'.",
                    },
                },
                "required": ["prompt"],
                "additionalProperties": False,
            },
        ),
    }

    @staticmethod
    def classify(question: str) -> str:
        rules = {
            "语音生成": ("转成语音", "朗读", "播报", "语音播放", "文字转语音", "tts", "读出来", "念出来"),
            "图片生成": ("生成图片", "画一张", "生成一张", "文生图", "画个", "绘制", "生成图"),
            "工单统计": ("待处理工单", "工单数量", "客服队列", "工单积压"),
            "系统故障": ("故障", "不可用", "报错", "中断", "崩溃"),
            "合同咨询": ("合同", "条款", "审批", "法务"),
            "发票办理": ("发票", "开票", "税号", "抬头"),
            "订单查询": ("订单", "交付", "履约", "验收", "进度"),
            "账户访问": ("登录", "密码", "账号", "邮箱", "账户"),
            "付款咨询": ("付款", "支付", "退款", "金额", "账单"),
        }
        return next((name for name, keywords in rules.items() if any(word in question for word in keywords)), "一般咨询")

    @classmethod
    def _tool_name_for_category(cls, category: str) -> str | None:
        return cls._TOOL_NAMES_BY_CATEGORY.get(category)

    @classmethod
    def _tool_definitions_for(cls, tool_name: str | None) -> tuple[LLMToolDefinition, ...]:
        if tool_name is None:
            return ()
        definition = cls._TOOL_DEFINITIONS.get(tool_name)
        return (definition,) if definition is not None else ()

    @classmethod
    def _tool_arguments_for(cls, tool_name: str | None, question: str) -> dict[str, object]:
        if tool_name != "support_ticket_queue_summary":
            return {}
        if "处理中" in question:
            return {"status": "in_progress"}
        if "已解决" in question or "已完成" in question:
            return {"status": "resolved"}
        return {"status": "open"}

    @classmethod
    def _execute_business_tool(
        cls,
        db: Session | None,
        tool_name: str | None,
        arguments: dict[str, object] | None = None,
    ) -> str | None:
        """Execute one whitelisted read-only tool after exact argument validation."""
        if tool_name is None:
            return None
        normalized_arguments = dict(arguments or {})
        if tool_name == "support_ticket_queue_summary":
            status = normalized_arguments.get("status")
            if db is None or set(normalized_arguments) != {"status"} or status not in {
                "open",
                "in_progress",
                "resolved",
            }:
                return None
            count = db.scalar(
                select(func.count(SupportTicket.id)).where(SupportTicket.status == status)
            ) or 0
            high_priority = db.scalar(
                select(func.count(SupportTicket.id)).where(
                    SupportTicket.status == status,
                    SupportTicket.priority.in_(("high", "urgent")),
                )
            ) or 0
            return (
                f"只读数据库查询结果：状态 {status} 的工单共 {count} 条，"
                f"其中 high/urgent 优先级 {high_priority} 条。结果仅含当前数据库聚合，不包含客户明细。"
            )
        if normalized_arguments:
            return None
        return cls._TOOL_RESULTS.get(tool_name)

    @classmethod
    def _select_provider_tool_call(
        cls,
        calls: Sequence[LLMToolCall],
        expected_tool_name: str | None,
        expected_arguments: dict[str, object] | None = None,
    ) -> LLMToolCall | None:
        """Accept one exact call from the category-specific whitelist."""
        if expected_tool_name is None:
            return None
        normalized_arguments = dict(expected_arguments or {})
        for call in calls:
            if call.name == expected_tool_name and dict(call.arguments) == normalized_arguments:
                return call
        return None

    @classmethod
    def _route_after_retrieval(cls, state: AgentWorkflowState) -> str:
        return "tool" if cls._tool_name_for_category(state["category"]) else "finish"

    @classmethod
    def business_tool(cls, category: str, question: str, db: Session | None = None) -> str | None:
        """Backward-compatible deterministic business-tool entry point."""
        tool_name = cls._tool_name_for_category(category)
        return cls._execute_business_tool(db, tool_name, cls._tool_arguments_for(tool_name, question))

    @staticmethod
    def _fallback_reply(category: str, hits: list[RetrievalHit], tool_result: str | None) -> str:
        sections: list[str] = [f"我将该问题归类为“{category}”。"]
        if hits:
            sections.append(f"根据《{hits[0].title}》，{hits[0].excerpt}")
        else:
            sections.append("当前知识库没有足够依据来确认具体情况，避免给出未经核实的承诺。")
        if tool_result:
            sections.append(tool_result)
        sections.append("建议：如需查询具体客户、订单或合同信息，请补充必要标识后转人工服务人员核验。")
        return "\n\n".join(sections)

    @staticmethod
    def _quality_check(answer: str, has_sources: bool) -> tuple[str, str]:
        if len(answer.strip()) < 30:
            return "fallback", "回复过短，已补充转人工建议"
        if not has_sources:
            return "fallback", "未检索到知识依据，已标记为谨慎答复"
        return "completed", "回复包含知识依据与下一步建议"

    def _run_langgraph(
        self,
        db: Session,
        question: str,
        top_k: int,
        cache_ttl_seconds: int,
        assistant_prompt: str,
    ) -> AgentWorkflowState:
        """Coordinate dedicated local agents with a real LangGraph StateGraph.

        Provider completion remains outside this synchronous graph so a remote-model
        timeout cannot interrupt classification, retrieval, tool safety, or prompt
        construction. The graph still records explicit handoffs between all agents.
        """
        if not LANGGRAPH_AVAILABLE or StateGraph is None:
            raise RuntimeError("LangGraph is not installed")

        def planner_node(_: AgentWorkflowState) -> AgentWorkflowState:
            return {
                "tasks": ["classify_intent", "retrieve_knowledge"],
                "plan_attempts": 0,
            }

        def classify_node(state: AgentWorkflowState) -> AgentWorkflowState:
            return {"category": self.classification_agent.run(state["question"])}

        def retrieval_node(state: AgentWorkflowState) -> AgentWorkflowState:
            return {
                "hits": self.knowledge_query_agent.run(
                    db,
                    state["question"],
                    state["top_k"],
                    state["cache_ttl_seconds"],
                )
            }

        def tool_node(state: AgentWorkflowState) -> AgentWorkflowState:
            tool_name = self._tool_name_for_category(state["category"])
            return {
                "tool_name": tool_name,
                "tool_arguments": self._tool_arguments_for(tool_name, state["question"]),
            }

        def parallel_join_node(_: AgentWorkflowState) -> AgentWorkflowState:
            return {}

        def response_plan_node(state: AgentWorkflowState) -> AgentWorkflowState:
            tool_name = state.get("tool_name")
            tool_arguments = state.get("tool_arguments", {})
            return {
                "response_plan": self.response_agent.prepare(
                    assistant_prompt=state["assistant_prompt"],
                    question=state["question"],
                    category=state["category"],
                    hits=state.get("hits", []),
                    tool_result=self._execute_business_tool(db, tool_name, tool_arguments),
                ),
                "plan_attempts": state.get("plan_attempts", 0) + 1,
            }

        def response_plan_quality_node(state: AgentWorkflowState) -> AgentWorkflowState:
            response_plan = state.get("response_plan", {})
            system_prompt = response_plan.get("system_prompt", "").strip()
            user_prompt = response_plan.get("user_prompt", "").strip()
            return {
                "plan_valid": bool(
                    system_prompt
                    and user_prompt
                    and "用户问题" in user_prompt
                    and ("企业知识" in user_prompt or "未检索到可用来源" in user_prompt)
                )
            }

        def route_after_plan_quality(state: AgentWorkflowState) -> str:
            if state.get("plan_valid") or state.get("plan_attempts", 0) >= 2:
                return "finish"
            return "retry"

        def finish_node(_: AgentWorkflowState) -> AgentWorkflowState:
            return {}

        workflow = StateGraph(AgentWorkflowState)
        workflow.add_node("task_planner", planner_node)
        workflow.add_node("classification_agent", classify_node)
        workflow.add_node("knowledge_query_agent", retrieval_node)
        workflow.add_node("parallel_join", parallel_join_node)
        workflow.add_node("business_tool_agent", tool_node)
        workflow.add_node("response_agent", response_plan_node)
        workflow.add_node("response_plan_quality", response_plan_quality_node)
        workflow.add_node("finish", finish_node)
        workflow.set_entry_point("task_planner")
        workflow.add_edge("task_planner", "classification_agent")
        workflow.add_edge("task_planner", "knowledge_query_agent")
        workflow.add_edge(["classification_agent", "knowledge_query_agent"], "parallel_join")
        workflow.add_conditional_edges(
            "parallel_join",
            self._route_after_retrieval,
            {"tool": "business_tool_agent", "finish": "response_agent"},
        )
        workflow.add_edge("business_tool_agent", "response_agent")
        workflow.add_edge("response_agent", "response_plan_quality")
        workflow.add_conditional_edges(
            "response_plan_quality",
            route_after_plan_quality,
            {"retry": "response_agent", "finish": "finish"},
        )
        workflow.add_edge("finish", END)
        return workflow.compile().invoke(
            {
                "question": question,
                "top_k": top_k,
                "cache_ttl_seconds": cache_ttl_seconds,
                "assistant_prompt": assistant_prompt,
            }
        )

    @staticmethod
    def _conversation_history(
        db: Session,
        conversation_id: int | None,
        message_limit: int,
        current_question: str,
    ) -> list[LLMHistoryMessage]:
        """Load only the recent turns of the current conversation for an LLM call."""
        if conversation_id is None or message_limit <= 0:
            return []
        rows = db.execute(
            select(Message.role, Message.content)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.id.desc())
            .limit(message_limit + _SUMMARY_SOURCE_MESSAGES + 1)
        ).all()
        history = [
            LLMHistoryMessage(role=role, content=content)
            for role, content in reversed(rows)
            if role in {"user", "assistant"} and content.strip()
        ]
        # `_run_chat` persists the newest user message before invoking the agent. It
        # remains the final user prompt rather than being sent twice as history.
        if history and history[-1].role == "user" and history[-1].content.strip() == current_question.strip():
            history.pop()
        recent = history[-message_limit:]
        older = history[:-message_limit]
        if not older:
            return recent

        prefix = "较早会话摘要（自动提取）："
        pieces: list[str] = []
        remaining = _SUMMARY_MAX_CHARS - len(prefix)
        for message in older:
            label = "用户" if message.role == "user" else "助手"
            content = " ".join(message.content.split())
            part = f"{label}：{content[:180]}"
            separator_length = 3 if pieces else 0
            if len(part) + separator_length > remaining:
                available = max(remaining - separator_length, 0)
                if available:
                    pieces.append(part[:available])
                break
            pieces.append(part)
            remaining -= len(part) + separator_length
        summary = LLMHistoryMessage(role="system", content=prefix + " | ".join(pieces))
        return [summary, *recent]

    @staticmethod
    def _effective_top_k(runtime_settings: RuntimeSettings, top_k: int | None) -> int:
        if top_k is None:
            return runtime_settings.retrieval_top_k
        if not 1 <= top_k <= 8:
            raise ValueError("top_k must be between 1 and 8")
        return top_k

    async def _complete(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        history: Sequence[LLMHistoryMessage],
        tools: Sequence[LLMToolDefinition] = (),
        tool_results: Sequence[LLMToolResult] = (),
        model: str | None = None,
    ) -> Completion:
        """Use tool calls only with clients that explicitly support the extension."""
        if isinstance(self.llm_client, OpenAICompatibleClient):
            if tools or tool_results:
                return await self.llm_client.complete(
                    system_prompt,
                    user_prompt,
                    history=history,
                    tools=tools,
                    tool_results=tool_results,
                    model=model,
                )
            return await self.llm_client.complete(system_prompt, user_prompt, history=history, model=model)
        if (tools or tool_results) and getattr(self.llm_client, "supports_tool_calls", False):
            return await self.llm_client.complete(
                system_prompt,
                user_prompt,
                history=history,
                tools=tools,
                tool_results=tool_results,
            )
        return await self.llm_client.complete(system_prompt, user_prompt, history=history)

    async def _stream_complete(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        history: Sequence[LLMHistoryMessage],
        tools: Sequence[LLMToolDefinition] = (),
        tool_results: Sequence[LLMToolResult] = (),
        model: str | None = None,
    ) -> AsyncIterator[LLMStreamTextDelta | LLMStreamToolCallDelta | LLMStreamCompleted]:
        stream_method = getattr(self.llm_client, "stream_complete", None)
        if stream_method is None:
            completion = await self._complete(
                system_prompt,
                user_prompt,
                history=history,
                tools=tools,
                tool_results=tool_results,
                model=model,
            )
            yield LLMStreamCompleted(completion)
            return

        kwargs: dict[str, object] = {"history": history}
        if isinstance(self.llm_client, OpenAICompatibleClient):
            kwargs.update({"tools": tools, "tool_results": tool_results, "model": model})
        elif (tools or tool_results) and getattr(self.llm_client, "supports_tool_calls", False):
            kwargs.update({"tools": tools, "tool_results": tool_results})

        provider_stream = stream_method(system_prompt, user_prompt, **kwargs)
        try:
            async for event in provider_stream:
                yield event
        finally:
            close = getattr(provider_stream, "aclose", None)
            if close is not None:
                await close()

    async def stream(
        self,
        db: Session,
        question: str,
        top_k: int | None = None,
        *,
        conversation_id: int | None = None,
        preference_instruction: str | None = None,
    ) -> AsyncIterator[AgentStreamTrace | AgentStreamToken | AgentStreamReset | AgentStreamCompleted]:
        runtime_settings = get_runtime_settings(db)
        effective_top_k = self._effective_top_k(runtime_settings, top_k)
        assistant_prompt = runtime_settings.assistant_prompt
        admin_instruction = runtime_prompt_instruction(runtime_settings)
        if admin_instruction:
            assistant_prompt = f"{assistant_prompt}\n{admin_instruction}"
        if preference_instruction and preference_instruction.strip():
            assistant_prompt = (
                f"{assistant_prompt}\n用户偏好（只影响表达方式，不改变事实、安全或权限边界）："
                f"{preference_instruction.strip()}"
            )
        trace: list[AgentTrace] = []
        graph_note = "本地状态编排"
        response_plan: dict[str, str] | None = None
        try:
            if LANGGRAPH_AVAILABLE:
                graph_state = self._run_langgraph(
                    db,
                    question,
                    effective_top_k,
                    runtime_settings.retrieval_cache_ttl_seconds,
                    assistant_prompt,
                )
                category = graph_state["category"]
                hits = graph_state["hits"]
                tool_name = graph_state.get("tool_name")
                tool_arguments = graph_state.get("tool_arguments", {})
                response_plan = graph_state.get("response_plan")
                graph_note = "LangGraph 受控任务分解 + 并行分类/检索 + 条件路由/有界循环"
            else:
                raise RuntimeError("LangGraph optional dependency is unavailable")
        except (KeyError, RuntimeError, TypeError, ValueError) as error:
            category = self.classify(question)
            hits = retrieve(
                db,
                question,
                top_k=effective_top_k,
                cache_ttl_seconds=runtime_settings.retrieval_cache_ttl_seconds,
            )
            tool_name = self._tool_name_for_category(category)
            tool_arguments = self._tool_arguments_for(tool_name, question)
            graph_note = f"本地状态编排（{type(error).__name__} 回退）"

        tool_result = self._execute_business_tool(db, tool_name, tool_arguments)
        if response_plan is None:
            response_plan = self.response_agent.prepare(
                assistant_prompt=assistant_prompt,
                question=question,
                category=category,
                hits=hits,
                tool_result=tool_result,
            )

        classification_trace = AgentTrace(
            step="分类 Agent",
            status="completed",
            detail=f"识别为：{category}；{graph_note}",
        )
        trace.append(classification_trace)
        yield AgentStreamTrace(classification_trace)

        citations = [
            Citation(document_id=hit.document_id, title=hit.title, excerpt=hit.excerpt, score=hit.score)
            for hit in hits
        ]
        retrieval_trace = AgentTrace(
            step="知识检索 Agent",
            status="completed" if hits else "fallback",
            detail=(
                f"命中 {len(citations)} 条企业知识片段；"
                f"top_k={effective_top_k}，缓存 {runtime_settings.retrieval_cache_ttl_seconds} 秒"
            ),
        )
        trace.append(retrieval_trace)
        yield AgentStreamTrace(retrieval_trace)

        system_prompt = response_plan["system_prompt"]
        user_prompt = response_plan["user_prompt"]
        history: Sequence[LLMHistoryMessage] = self._conversation_history(
            db,
            conversation_id,
            runtime_settings.conversation_memory_messages,
            question,
        )
        provider_tools = self._tool_definitions_for(tool_name)
        answer_parts: list[str] = []
        completion: Completion | None = None
        first_phase: Literal["text", "tool"] | None = None
        mixed_provider_output = False

        async for event in self._stream_complete(
            system_prompt,
            user_prompt,
            history=history,
            tools=provider_tools,
            model=runtime_settings.llm_model,
        ):
            if isinstance(event, LLMStreamTextDelta):
                if first_phase in {None, "text"}:
                    first_phase = "text"
                    answer_parts.append(event.text)
                    yield AgentStreamToken(event.text, "model")
                else:
                    mixed_provider_output = True
            elif isinstance(event, LLMStreamToolCallDelta):
                if first_phase is None:
                    first_phase = "tool"
                elif first_phase == "text":
                    mixed_provider_output = True
            elif isinstance(event, LLMStreamCompleted):
                completion = event.completion

        if completion is None:
            completion = Completion(text=None, used_fallback=True, reason="模型流未返回完成事件")

        tool_route_fallback = mixed_provider_output
        tool_detail = tool_result or "该意图无需调用业务工具"
        if mixed_provider_output:
            tool_detail = f"模型流同时返回正文和工具调用，已按本地安全规则回退；{tool_detail}"
        elif provider_tools:
            if completion.tool_call_parse_failed:
                tool_route_fallback = True
                tool_detail = f"模型工具调用格式无效，已按本地安全规则回退；{tool_detail}"
            elif completion.tool_calls:
                selected_call = self._select_provider_tool_call(
                    completion.tool_calls,
                    tool_name,
                    tool_arguments,
                )
                if selected_call is None:
                    tool_route_fallback = True
                    tool_detail = f"模型请求了未授权工具，已按本地安全规则回退；{tool_detail}"
                else:
                    selected_result = self._execute_business_tool(
                        db,
                        selected_call.name,
                        dict(selected_call.arguments),
                    )
                    if selected_result is None:
                        tool_route_fallback = True
                        tool_detail = f"模型工具执行不可用，已按本地安全规则回退；{tool_detail}"
                    else:
                        tool_result = selected_result
                        tool_detail = f"模型函数调用 {selected_call.name} 已通过白名单校验；{tool_result}"
                        second_completion: Completion | None = None
                        second_stream_invalid = False
                        async for second_event in self._stream_complete(
                            system_prompt,
                            user_prompt,
                            history=history,
                            tool_results=(LLMToolResult(call=selected_call, content=tool_result),),
                            model=runtime_settings.llm_model,
                        ):
                            if isinstance(second_event, LLMStreamTextDelta):
                                answer_parts.append(second_event.text)
                                yield AgentStreamToken(second_event.text, "model")
                            elif isinstance(second_event, LLMStreamToolCallDelta):
                                second_stream_invalid = True
                            elif isinstance(second_event, LLMStreamCompleted):
                                second_completion = second_event.completion
                        if second_completion is None:
                            second_completion = Completion(
                                text=None,
                                used_fallback=True,
                                reason="工具结果后的模型流未返回完成事件",
                            )
                        if (
                            second_stream_invalid
                            or second_completion.tool_call_parse_failed
                            or second_completion.tool_calls
                        ):
                            tool_route_fallback = True
                            tool_detail = f"工具结果后的模型流格式无效，已按本地安全规则回退；{tool_detail}"
                        completion = second_completion
            elif first_phase == "tool":
                tool_route_fallback = True
                tool_detail = f"模型工具调用缺少有效参数，已按本地安全规则回退；{tool_detail}"
        elif completion.tool_calls or completion.tool_call_parse_failed or first_phase == "tool":
            tool_route_fallback = True
            tool_detail = "模型返回了未提供的工具调用，已按本地安全规则回退"

        streamed_answer = "".join(answer_parts)
        generation_fallback = completion.used_fallback or tool_route_fallback
        if generation_fallback or not (streamed_answer or completion.text):
            answer = self._fallback_reply(category, hits, tool_result)
            generation_fallback = True
            if streamed_answer:
                yield AgentStreamReset(answer)
            else:
                yield AgentStreamToken(answer, "fallback")
        else:
            answer = streamed_answer or completion.text or ""
            if not streamed_answer and answer:
                yield AgentStreamReset(answer)

        business_tool_trace = AgentTrace(
            step="业务工具 Agent",
            status="fallback" if tool_route_fallback else "completed" if tool_result else "skipped",
            detail=tool_detail,
        )
        trace.append(business_tool_trace)
        yield AgentStreamTrace(business_tool_trace)

        response_trace = AgentTrace(
            step="回复 Agent",
            status="fallback" if generation_fallback else "completed",
            detail=completion.reason or (
                "已按本地安全规则生成回退回复" if generation_fallback else "已通过模型原生流生成基于知识的回复"
            ),
        )
        trace.append(response_trace)
        yield AgentStreamTrace(response_trace)

        quality_status, quality_detail = self.quality_agent.run(answer, bool(hits))
        if quality_status == "fallback" and "建议" not in answer:
            answer = f"{answer}\n\n建议：请转人工服务人员核验后继续处理。"
            yield AgentStreamReset(answer)
        quality_trace = AgentTrace(step="质检 Agent", status=quality_status, detail=quality_detail)
        trace.append(quality_trace)
        yield AgentStreamTrace(quality_trace)

        email_draft = self.email_draft_agent.run(category, question)
        email_trace = AgentTrace(
            step="邮件草稿 Agent",
            status="completed" if email_draft else "skipped",
            detail=email_draft or "当前意图无需生成升级邮件草稿",
        )
        trace.append(email_trace)
        yield AgentStreamTrace(email_trace)
        yield AgentStreamCompleted(
            AgentResult(
                answer=answer,
                citations=citations,
                trace=trace,
                used_fallback=generation_fallback or quality_status == "fallback",
                category=category,
            )
        )

    async def run(
        self,
        db: Session,
        question: str,
        top_k: int | None = None,
        *,
        conversation_id: int | None = None,
        preference_instruction: str | None = None,
    ) -> AgentResult:
        runtime_settings = get_runtime_settings(db)
        effective_top_k = self._effective_top_k(runtime_settings, top_k)
        assistant_prompt = runtime_settings.assistant_prompt
        admin_instruction = runtime_prompt_instruction(runtime_settings)
        if admin_instruction:
            assistant_prompt = f"{assistant_prompt}\n{admin_instruction}"
        if preference_instruction and preference_instruction.strip():
            assistant_prompt = (
                f"{assistant_prompt}\n用户偏好（只影响表达方式，不改变事实、安全或权限边界）："
                f"{preference_instruction.strip()}"
            )
        trace: list[AgentTrace] = []
        graph_note = "本地状态编排"
        response_plan: dict[str, str] | None = None
        try:
            if LANGGRAPH_AVAILABLE:
                graph_state = self._run_langgraph(
                    db,
                    question,
                    effective_top_k,
                    runtime_settings.retrieval_cache_ttl_seconds,
                    assistant_prompt,
                )
                category = graph_state["category"]
                hits = graph_state["hits"]
                tool_name = graph_state.get("tool_name")
                tool_arguments = graph_state.get("tool_arguments", {})
                response_plan = graph_state.get("response_plan")
                graph_note = "LangGraph 受控任务分解 + 并行分类/检索 + 条件路由/有界循环"
            else:
                raise RuntimeError("LangGraph optional dependency is unavailable")
        except (KeyError, RuntimeError, TypeError, ValueError) as error:
            category = self.classify(question)
            hits = retrieve(
                db,
                question,
                top_k=effective_top_k,
                cache_ttl_seconds=runtime_settings.retrieval_cache_ttl_seconds,
            )
            tool_name = self._tool_name_for_category(category)
            tool_arguments = self._tool_arguments_for(tool_name, question)
            graph_note = f"本地状态编排（{type(error).__name__} 回退）"

        tool_result = self._execute_business_tool(db, tool_name, tool_arguments)
        if response_plan is None:
            response_plan = self.response_agent.prepare(
                assistant_prompt=assistant_prompt,
                question=question,
                category=category,
                hits=hits,
                tool_result=tool_result,
            )

        trace.append(AgentTrace(step="分类 Agent", status="completed", detail=f"识别为：{category}；{graph_note}"))
        citations = [
            Citation(document_id=hit.document_id, title=hit.title, excerpt=hit.excerpt, score=hit.score)
            for hit in hits
        ]
        trace.append(
            AgentTrace(
                step="知识检索 Agent",
                status="completed" if hits else "fallback",
                detail=(
                    f"命中 {len(citations)} 条企业知识片段；"
                    f"top_k={effective_top_k}，缓存 {runtime_settings.retrieval_cache_ttl_seconds} 秒"
                ),
            )
        )

        system_prompt = response_plan["system_prompt"]
        user_prompt = response_plan["user_prompt"]
        history: Sequence[LLMHistoryMessage] = self._conversation_history(
            db,
            conversation_id,
            runtime_settings.conversation_memory_messages,
            question,
        )
        provider_tools = self._tool_definitions_for(tool_name)
        completion = await self._complete(
            system_prompt,
            user_prompt,
            history=history,
            tools=provider_tools,
            model=runtime_settings.llm_model,
        )
        tool_route_fallback = False
        tool_detail = tool_result or "该意图无需调用业务工具"
        if provider_tools:
            if completion.tool_call_parse_failed:
                tool_route_fallback = True
                tool_detail = f"模型工具调用格式无效，已按本地安全规则回退；{tool_detail}"
            elif completion.tool_calls:
                selected_call = self._select_provider_tool_call(
                    completion.tool_calls,
                    tool_name,
                    tool_arguments,
                )
                if selected_call is None:
                    tool_route_fallback = True
                    tool_detail = f"模型请求了未授权工具，已按本地安全规则回退；{tool_detail}"
                else:
                    selected_result = self._execute_business_tool(
                        db,
                        selected_call.name,
                        dict(selected_call.arguments),
                    )
                    if selected_result is None:  # Defensive: the whitelist should make this unreachable.
                        tool_route_fallback = True
                        tool_detail = f"模型工具执行不可用，已按本地安全规则回退；{tool_detail}"
                    else:
                        tool_result = selected_result
                        tool_detail = f"模型函数调用 {selected_call.name} 已通过白名单校验；{tool_result}"
                        completion = await self._complete(
                            system_prompt,
                            user_prompt,
                            history=history,
                            tool_results=(LLMToolResult(call=selected_call, content=tool_result),),
                            model=runtime_settings.llm_model,
                        )
        trace.append(
            AgentTrace(
                step="业务工具 Agent",
                status="fallback" if tool_route_fallback else "completed" if tool_result else "skipped",
                detail=tool_detail,
            )
        )
        answer = completion.text or self._fallback_reply(category, hits, tool_result)
        trace.append(
            AgentTrace(
                step="回复 Agent",
                status="fallback" if completion.used_fallback else "completed",
                detail=completion.reason or "已通过模型生成基于知识的回复",
            )
        )

        quality_status, quality_detail = self.quality_agent.run(answer, bool(hits))
        if quality_status == "fallback" and "建议" not in answer:
            answer = f"{answer}\n\n建议：请转人工服务人员核验后继续处理。"
        trace.append(AgentTrace(step="质检 Agent", status=quality_status, detail=quality_detail))
        email_draft = self.email_draft_agent.run(category, question)
        trace.append(
            AgentTrace(
                step="邮件草稿 Agent",
                status="completed" if email_draft else "skipped",
                detail=email_draft or "当前意图无需生成升级邮件草稿",
            )
        )
        return AgentResult(
            answer=answer,
            citations=citations,
            trace=trace,
            used_fallback=completion.used_fallback or tool_route_fallback or quality_status == "fallback",
            category=category,
        )

    async def run_support_assistant(
        self,
        db: Session,
        question: str,
        *,
        conversation_id: int | None = None,
        use_knowledge: bool = False,
    ) -> AgentResult:
        """Generate a reviewable support draft with an optional knowledge hint.

        This intentionally does not call :meth:`run`: the enterprise assistant
        is required to be fully grounded, while a support agent needs a broader
        drafting model.  Retrieved snippets are labelled as references and can
        be disabled per request; the result is never sent to a customer here.
        """
        runtime_settings = get_runtime_settings(db)
        category = self.classify(question)
        hits = (
            retrieve(
                db,
                question,
                top_k=runtime_settings.retrieval_top_k,
                cache_ttl_seconds=runtime_settings.retrieval_cache_ttl_seconds,
            )
            if use_knowledge
            else []
        )
        citations = [
            Citation(document_id=hit.document_id, title=hit.title, excerpt=hit.excerpt, score=hit.score)
            for hit in hits
        ]
        prompt = runtime_settings.support_assistant_prompt or DEFAULT_ASSISTANT_PROMPT
        admin_instruction = runtime_prompt_instruction(runtime_settings)
        if admin_instruction:
            prompt = f"{prompt}\n{admin_instruction}"
        reference_text = "\n".join(f"[{hit.title}] {hit.excerpt}" for hit in hits)
        if not reference_text:
            reference_text = "（本次未提供企业知识片段；请使用通用业务判断并明确需要人工核验的部分。）"
        user_prompt = (
            f"客户问题：{question.strip()}\n"
            f"问题分类：{category}\n"
            f"可选知识参考（不是唯一事实来源）：{reference_text}\n"
            "请输出一份简洁、礼貌、可执行的客服回复草稿；标出不确定信息，"
            "不要声称已经发送，也不要编造订单、金额或个人信息。"
        )
        history = self._conversation_history(
            db,
            conversation_id,
            runtime_settings.conversation_memory_messages,
            question,
        )
        completion = await self._complete(
            prompt,
            user_prompt,
            history=history,
            model=runtime_settings.support_assistant_model,
        )
        used_fallback = completion.used_fallback or not completion.text
        if used_fallback:
            answer = (
                f"客服辅助草稿（需人工确认）：该问题可归类为“{category}”。"
                "建议先复述客户诉求，核对必要信息，再依据企业流程回复；"
                "涉及账户、订单、合同或资金的信息请由客服人工核验。"
            )
        else:
            answer = completion.text or ""
        trace = [
            AgentTrace(
                step="客服辅助 Agent",
                status="fallback" if used_fallback else "completed",
                detail=(
                    completion.reason or "使用独立客服辅助模型生成草稿，结果不会自动发送"
                ),
            ),
            AgentTrace(
                step="可选知识参考",
                status="completed" if hits else "skipped",
                detail=(
                    f"提供 {len(hits)} 条知识片段作为参考；模型允许结合通用知识"
                    if hits
                    else "本次未使用企业知识库，模型按通用客服策略生成"
                ),
            ),
        ]
        return AgentResult(
            answer=answer,
            citations=citations,
            trace=trace,
            used_fallback=used_fallback,
            category=category,
        )

    # ------------------------------------------------------------------
    # Dify callback support: media tools + LangGraph callback entry point
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_last_assistant_text(
        context: list[dict[str, str]] | None,
        db: Session | None = None,
        conversation_id: int | None = None,
    ) -> str | None:
        """Resolve '上一条回复' / '这段回复' from context or conversation history."""
        if context:
            for msg in reversed(context):
                if msg.get("role") == "assistant" and msg.get("content", "").strip():
                    return msg["content"].strip()
        if db is not None and conversation_id is not None:
            row = db.execute(
                select(Message.content)
                .where(Message.conversation_id == conversation_id, Message.role == "assistant")
                .order_by(Message.id.desc())
                .limit(1)
            ).scalar_one_or_none()
            if row and row.strip():
                return row.strip()
        return None

    async def _execute_media_tool(
        self,
        tool_name: str,
        arguments: dict[str, object],
        user_id: str,
    ) -> tuple[str, list[Artifact]]:
        """Execute a Dify media tool asynchronously. Returns (tool_result_text, artifacts)."""
        if tool_name == "dify_text_to_speech":
            if not set(arguments) <= {"text", "voice"} or "text" not in arguments:
                return "语音生成失败：工具参数不合法。", []
            text_value = arguments.get("text")
            voice_value = arguments.get("voice", "Cherry")
            if not isinstance(text_value, str) or not isinstance(voice_value, str):
                return "语音生成失败：工具参数不合法。", []
            text = text_value.strip()
            voice = voice_value.strip()
            if not voice or voice.casefold() == "default":
                voice = "Cherry"
            if not text or len(text) > 6000 or len(voice) > 40:
                return "语音生成失败：文本或音色参数不合法。", []
            result = await self.dify_gateway.run_text_to_speech(text, voice, user_id)
            if result.degraded or (not result.media_url and not result.data_url):
                return f"语音生成服务暂不可用：{result.detail}", []
            artifact = Artifact(
                kind="audio",
                media_url=result.media_url,
                data_url=result.data_url,
                content_type=result.content_type,
                byte_size=result.byte_size,
            )
            return "语音生成成功，已返回可播放音频。", [artifact]

        if tool_name == "dify_text_to_image":
            if not set(arguments) <= {"prompt", "size"} or "prompt" not in arguments:
                return "图片生成失败：工具参数不合法。", []
            prompt_value = arguments.get("prompt")
            size_value = arguments.get("size", "1024x1024")
            if not isinstance(prompt_value, str) or not isinstance(size_value, str):
                return "图片生成失败：工具参数不合法。", []
            prompt = prompt_value.strip()
            size = size_value.strip() or "1024x1024"
            if not prompt or len(prompt) > 2000:
                return "图片生成失败：提示词为空或超过2000字符限制。", []
            valid_sizes = {
                "1024x1024",
                "1280x720",
                "720x1280",
                "2048*2048",
                "2688*1536",
                "1536*2688",
            }
            if size not in valid_sizes:
                return "图片生成失败：画幅参数不合法。", []
            result = await self.dify_gateway.run_text_to_image(prompt, size, user_id)
            if result.degraded or (not result.media_url and not result.data_url):
                return f"图片生成服务暂不可用：{result.detail}", []
            artifact = Artifact(
                kind="image",
                media_url=result.media_url,
                data_url=result.data_url,
                content_type=result.content_type,
                byte_size=result.byte_size,
            )
            return "图片生成成功，已返回图片。", [artifact]

        return f"未知媒体工具：{tool_name}", []

    async def run_callback(
        self,
        db: Session,
        query: str,
        *,
        context: list[dict[str, str]] | None = None,
        conversation_id: int | None = None,
        user_id: str = "0",
        route: str = "complex",
        media_intent: str | None = None,
    ) -> AgentResult:
        """Entry point for Dify router workflow HTTP callback.

        Handles both 'complex' (LangGraph multi-step) and 'media' (TTS/image)
        routes.  Does NOT call the Dify router workflow (no recursion).
        """
        category = self.classify(query)
        trace: list[AgentTrace] = []
        artifacts: list[Artifact] = []

        # Media route: resolve text reference and call Dify media tools directly.
        if route == "media" or category in ("语音生成", "图片生成"):
            if route == "media":
                if media_intent == "image":
                    category = "图片生成"
                elif media_intent == "tts":
                    category = "语音生成"
                elif category not in ("语音生成", "图片生成"):
                    image_markers = ("图片", "画一张", "画个", "绘制", "文生图")
                    category = "图片生成" if any(marker in query for marker in image_markers) else "语音生成"
            tool_name = self._tool_name_for_category(category)
            if tool_name is None:  # Defensive: media categories are always whitelisted above.
                return AgentResult(
                    answer="无法识别媒体生成类型，请明确说明需要语音还是图片。",
                    citations=[],
                    trace=[AgentTrace(step="媒体工具 Agent", status="fallback", detail="媒体类型无效")],
                    used_fallback=True,
                    category=category,
                    artifacts=[],
                )

            arguments: dict[str, object] = {}
            if tool_name == "dify_text_to_speech":
                # Resolve "上一条回复" / "这段回复" from context or DB.
                resolved_text = self._resolve_last_assistant_text(context, db, conversation_id)
                # If the query itself contains substantial text to convert, use it.
                explicit_text = query
                for phrase in ("帮我把", "请把", "将", "转成语音", "朗读", "转语音"):
                    explicit_text = explicit_text.replace(phrase, "")
                explicit_text = explicit_text.strip().strip("\"'")
                references_reply = any(
                    phrase in query for phrase in ("上一条回复", "这段回复", "上条回复", "刚才的回复")
                )
                text_to_speak = (
                    resolved_text
                    if resolved_text and (references_reply or not explicit_text or len(explicit_text) < 4)
                    else explicit_text
                )
                if not text_to_speak:
                    return AgentResult(
                        answer="未找到可转换的文本内容。请先发送一条需要朗读的回复，或直接提供要转换的文字。",
                        citations=[],
                        trace=[AgentTrace(step="媒体工具 Agent", status="fallback", detail="无可朗读文本")],
                        used_fallback=True,
                        category=category,
                        artifacts=[],
                    )
                arguments = {"text": text_to_speak[:6000], "voice": "Cherry"}
            else:
                # Image: extract prompt from query.
                prompt_text = query
                for phrase in ("帮我", "请", "生成一张", "生成图片", "画一张", "画个", "绘制", "文生图"):
                    prompt_text = prompt_text.replace(phrase, "")
                prompt_text = prompt_text.strip().strip("\"'") or query
                arguments = {"prompt": prompt_text[:2000], "size": "1024x1024"}

            tool_result, artifacts = await self._execute_media_tool(tool_name, arguments, user_id)
            trace.append(AgentTrace(
                step="媒体工具 Agent",
                status="completed" if artifacts else "fallback",
                detail=tool_result,
            ))
            answer = tool_result
            if artifacts:
                kind_label = "语音" if artifacts[0].kind == "audio" else "图片"
                answer = f"已为你生成{kind_label}。"
            return AgentResult(
                answer=answer,
                citations=[],
                trace=trace,
                used_fallback=not artifacts,
                category=category,
                artifacts=artifacts,
            )

        # Complex route: run the full LangGraph orchestration (no Dify router call).
        result = await self.run(db, query, conversation_id=conversation_id)
        return result
