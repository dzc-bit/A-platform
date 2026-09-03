from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import replace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings as app_settings
from app.database import Base
from app.models import Order, SupportTicket
from app.services import agent as agent_service
from app.services.agent import (
    AgentStreamCompleted,
    AgentStreamToken,
    AssistantWorkflow,
    GroundednessGate,
    IntentRouter,
    QueryRewriter,
    keyword_classify,
)
from app.services.knowledge import RetrievalHit
from app.services.llm import (
    Completion,
    LLMHistoryMessage,
    LLMStreamCompleted,
    LLMStreamTextDelta,
    LLMToolCall,
    LLMToolResult,
    OpenAICompatibleClient,
    parse_openai_tool_calls,
)
from app.services.seed import seed_demo_data


@pytest.fixture()
def seeded_db() -> Iterator[Session]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        seed_demo_data(session)
        yield session
    finally:
        session.close()
        engine.dispose()


def _ticket_count(db: Session) -> int:
    return int(db.scalar(select(func.count(SupportTicket.id))) or 0)


class ScriptedToolLLM:
    """Scripted multi-round fake: tool call first, final text after tool results."""

    supports_tool_calls = True

    def __init__(self, tool_name: str, arguments: dict[str, object]) -> None:
        self.tool_name = tool_name
        self.arguments = arguments
        self.rounds: list[dict[str, object]] = []

    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        history: Sequence[LLMHistoryMessage] = (),
        tools: Sequence[object] = (),
        tool_results: Sequence[LLMToolResult] = (),
    ) -> Completion:
        self.rounds.append(
            {
                "system_prompt": system_prompt,
                "tools": tuple(tool.name for tool in tools),  # type: ignore[attr-defined]
                "tool_results": tuple(result.call.name for result in tool_results),
            }
        )
        if tools and not tool_results:
            return Completion(
                text=None,
                used_fallback=False,
                tool_calls=(
                    LLMToolCall(id="call_scripted_1", name=self.tool_name, arguments=self.arguments),
                ),
            )
        assert tool_results, "expected the tool result to be fed back"
        return Completion(
            text="订单 A-1024 当前状态为履约中，服务开通与配置联调已完成，可按流程发起验收。",
            used_fallback=False,
        )


class AlwaysCallingLLM:
    """Keeps proposing the same (valid) tool call: exercises the bounded loop."""

    supports_tool_calls = True

    def __init__(self, tool_name: str, arguments: dict[str, object]) -> None:
        self.tool_name = tool_name
        self.arguments = arguments
        self.executions = 0

    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        history: Sequence[LLMHistoryMessage] = (),
        tools: Sequence[object] = (),
        tool_results: Sequence[LLMToolResult] = (),
    ) -> Completion:
        del system_prompt, user_prompt, history
        if tools:
            self.executions += 1
            return Completion(
                text=None,
                used_fallback=False,
                tool_calls=(LLMToolCall(id=f"call_loop_{self.executions}", name=self.tool_name, arguments=dict(self.arguments)),),
            )
        return Completion(text="已到达循环上限后的综合回答。", used_fallback=False)


class UntrustedToolLLM:
    """Proposes a non-whitelisted tool and must never see it executed."""

    supports_tool_calls = True

    def __init__(self) -> None:
        self.executed_results: list[str] = []

    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        history: Sequence[LLMHistoryMessage] = (),
        tools: Sequence[object] = (),
        tool_results: Sequence[LLMToolResult] = (),
    ) -> Completion:
        del system_prompt, user_prompt, history, tools
        if tool_results:
            self.executed_results.extend(result.content for result in tool_results)
            return Completion(text="已收到校验反馈，直接依据已有信息回答。", used_fallback=False)
        return Completion(
            text=None,
            used_fallback=False,
            tool_calls=(LLMToolCall(id="call_unsafe_1", name="exfiltrate_records", arguments={}),),
        )


class RecordingMediaGateway:
    def __init__(self) -> None:
        self.tts_calls: list[tuple[str, str, str]] = []

    async def run_text_to_speech(self, text: str, voice: str, user: str) -> object:
        self.tts_calls.append((text, voice, user))
        return SimpleMediaResult(kind="audio", media_url="https://media.example/a.mp3", degraded=False)


class SimpleMediaResult:
    def __init__(self, *, kind: str, media_url: str | None, degraded: bool) -> None:
        self.kind = kind
        self.media_url = media_url
        self.data_url = None
        self.content_type = "audio/mpeg" if kind == "audio" else "image/png"
        self.byte_size = 128
        self.degraded = degraded
        self.detail = "ok"


def test_keyword_rules_cover_all_categories_and_routes() -> None:
    assert keyword_classify("系统中断导致客户无法使用") == ("系统故障", "complex")
    assert keyword_classify("开票需要哪些资料") == ("发票办理", "knowledge")
    assert keyword_classify("帮我把这段话转成语音") == ("语音生成", "media")
    assert keyword_classify("随便聊聊") == ("一般咨询", "knowledge")
    assert AssistantWorkflow.classify("订单何时可以验收") == "订单查询"


def test_router_prompt_renders_single_source_rules() -> None:
    prompt = IntentRouter._SYSTEM_PROMPT
    assert '"route"' in prompt.replace(" ", "") or "route" in prompt
    for category in ("系统故障", "发票办理", "语音生成", "一般咨询"):
        assert category in prompt
    assert "knowledge / complex / media" in prompt
    # The rewrite prompt forbids answering and forbids inventing facts.
    rewrite_prompt = QueryRewriter._SYSTEM_PROMPT
    assert "只改写、不回答" in rewrite_prompt
    assert "不添加原问题没有的信息" in rewrite_prompt


def test_tool_agent_whitelist_executes_real_order_lookup(seeded_db: Session) -> None:
    workflow = AssistantWorkflow()
    accepted, result = workflow.tool_agent.validate_and_execute(
        seeded_db,
        LLMToolCall(id="call_1", name="order_status_lookup", arguments={"order_id": "A-1024"}),
    )

    assert accepted is True
    assert "A-1024" in result
    assert "履约中" in result
    assert "e***@neusoft.local" in result
    assert "价格" not in result


def test_tool_agent_rejects_unknown_tools_and_invalid_arguments(seeded_db: Session) -> None:
    workflow = AssistantWorkflow()

    unknown = workflow.tool_agent.validate_and_execute(
        seeded_db, LLMToolCall(id="c1", name="exfiltrate_records", arguments={})
    )
    bad_order = workflow.tool_agent.validate_and_execute(
        seeded_db, LLMToolCall(id="c2", name="order_status_lookup", arguments={"order_id": "drop table"})
    )
    bad_status = workflow.tool_agent.validate_and_execute(
        seeded_db,
        LLMToolCall(id="c3", name="support_ticket_queue_summary", arguments={"status": "all"}),
    )

    assert unknown[0] is False and "不在授权列表" in unknown[1]
    assert bad_order[0] is False and "被拒绝" in bad_order[1]
    assert bad_status[0] is False
    assert _ticket_count(seeded_db) == 3


def test_offline_complex_route_creates_real_manual_review_ticket(seeded_db: Session) -> None:
    workflow = AssistantWorkflow()
    result = asyncio.run(workflow.run(seeded_db, "服务突然崩溃了，客户全部无法访问"))

    assert _ticket_count(seeded_db) == 4
    ticket = seeded_db.scalars(
        select(SupportTicket).where(SupportTicket.category == "系统故障").order_by(SupportTicket.id.desc())
    ).first()
    assert ticket is not None
    assert ticket.priority == "high"
    assert ticket.status == "open"
    assert "#1" in result.answer or "#4" in result.answer
    assert result.category == "系统故障"
    assert any(item.step == "工具调用" and item.status == "completed" for item in result.trace)


def test_offline_order_route_reads_seeded_order_without_fabrication(seeded_db: Session) -> None:
    workflow = AssistantWorkflow()
    result = asyncio.run(workflow.run(seeded_db, "帮我查一下订单 A-1024 现在的进度"))
    assert "A-1024" in result.answer
    assert "履约中" in result.answer

    missing = asyncio.run(workflow.run(seeded_db, "帮我查一下订单 Z-9999 现在的进度"))
    assert "未找到订单号为 Z-9999" in missing.answer


def test_model_driven_tool_call_is_validated_then_answered(seeded_db: Session) -> None:
    client = ScriptedToolLLM("order_status_lookup", {"order_id": "A-1024"})
    workflow = AssistantWorkflow(llm_client=client)  # type: ignore[arg-type]
    result = asyncio.run(workflow.run(seeded_db, "订单 A-1024 什么时候可以验收"))

    assert len(client.rounds) == 2
    assert "order_status_lookup" in client.rounds[0]["tools"]  # type: ignore[operator]
    assert client.rounds[1]["tool_results"] == ("order_status_lookup",)  # type: ignore[operator]
    assert result.answer.startswith("订单 A-1024 当前状态为履约中")
    assert result.used_fallback is False
    assert any(item.step == "工具调用" and "执行成功" in item.detail for item in result.trace)


def test_untrusted_tool_call_is_rejected_and_never_executed(seeded_db: Session) -> None:
    client = UntrustedToolLLM()
    workflow = AssistantWorkflow(llm_client=client)  # type: ignore[arg-type]
    result = asyncio.run(workflow.run(seeded_db, "订单 A-1024 什么时候可以验收"))

    # Everything fed back to the model must be a rejection notice — never an
    # execution result for the non-whitelisted tool.
    assert client.executed_results and all("被拒绝" in item for item in client.executed_results)
    assert _ticket_count(seeded_db) == 3
    assert result.answer.startswith("已收到校验反馈，直接依据已有信息回答")
    assert any(item.step == "工具调用" and "参数被拒绝" in item.detail for item in result.trace)


def test_tool_agent_loop_is_bounded_after_three_rounds(seeded_db: Session) -> None:
    client = AlwaysCallingLLM("support_ticket_queue_summary", {"status": "open"})
    workflow = AssistantWorkflow(llm_client=client)  # type: ignore[arg-type]
    result = asyncio.run(workflow.run(seeded_db, "现在待处理工单有多少？"))

    # The loop hard-caps at 3 tool executions; the model then gets one last
    # no-execution-chance completion whose tool proposal triggers the final
    # no-tools synthesis call.
    assert client.executions == 4
    assert result.answer.startswith("已到达循环上限后的综合回答")
    tool_trace = next(item for item in result.trace if item.step == "工具调用")
    assert tool_trace.detail.count("执行成功") == 3
    # Three read-only executions of the same aggregate: no rows changed.
    assert _ticket_count(seeded_db) == 3
    # Three read-only executions of the same aggregate: no rows changed.
    assert _ticket_count(seeded_db) == 3


def test_streamed_tool_call_rounds_then_streams_final_answer(seeded_db: Session) -> None:
    class StreamScriptedLLM(ScriptedToolLLM):
        async def stream_complete(
            self,
            system_prompt: str,
            user_prompt: str,
            *,
            history: Sequence[LLMHistoryMessage] = (),
            tools: Sequence[object] = (),
            tool_results: Sequence[LLMToolResult] = (),
        ) -> AsyncIterator[object]:
            completion = await ScriptedToolLLM.complete(
                self, system_prompt, user_prompt, history=history, tools=tools, tool_results=tool_results
            )
            if completion.tool_calls:
                yield agent_service.LLMStreamToolCallDelta(index=0)
                yield LLMStreamCompleted(completion)
                return
            yield LLMStreamTextDelta(completion.text or "")
            yield LLMStreamCompleted(completion)

    client = StreamScriptedLLM("order_status_lookup", {"order_id": "A-1024"})

    async def collect() -> list[object]:
        return [
            event
            async for event in AssistantWorkflow(llm_client=client).stream(  # type: ignore[arg-type]
                seeded_db,
                "订单 A-1024 什么时候可以验收",
            )
        ]

    events = asyncio.run(collect())
    tokens = [event.text for event in events if isinstance(event, AgentStreamToken)]
    terminal = next(event for event in events if isinstance(event, AgentStreamCompleted))

    assert tokens == ["订单 A-1024 当前状态为履约中，服务开通与配置联调已完成，可按流程发起验收。"]
    assert terminal.result.answer == tokens[0]
    assert terminal.result.used_fallback is False


def test_media_fast_path_skips_router_and_calls_gateway(seeded_db: Session) -> None:
    gateway = RecordingMediaGateway()
    workflow = AssistantWorkflow(dify_gateway=gateway)  # type: ignore[arg-type]
    result = asyncio.run(workflow.run(seeded_db, "帮我把欢迎使用转成语音"))

    assert gateway.tts_calls == [("欢迎使用", "Cherry", "local")]
    assert result.artifacts[0].kind == "audio"
    assert result.used_fallback is False


def test_langgraph_follows_rewrite_route_retrieve_shape(seeded_db: Session) -> None:
    if not agent_service.LANGGRAPH_AVAILABLE:
        pytest.skip("LangGraph is not installed")

    workflow = AssistantWorkflow()
    state = workflow._run_langgraph(
        seeded_db,
        "客服队列有多少待处理工单？",
        rewritten=None,
        route=None,
        category=None,
        top_k=3,
        cache_ttl_seconds=0,
        assistant_prompt="只依据知识和只读数据库聚合回答。",
    )

    assert state["tasks"] == ["rewrite_query", "route_intent", "retrieve_knowledge"]
    assert state["category"] == "工单统计"
    assert state["route"] == "complex"
    assert state["tool_name"] == "support_ticket_queue_summary"
    assert state["tool_arguments"] == {"status": "open"}
    assert "只读数据库查询结果" in (state.get("tool_result") or "")
    assert state["plan_valid"] is True


def test_lcel_prompt_pipeline_grounded_and_cautious_branches() -> None:
    if not agent_service.LCEL_AVAILABLE:
        pytest.skip("LangChain LCEL is not installed")

    composer = AssistantWorkflow().prompt_composer
    grounded = composer.prepare(
        assistant_prompt="只依据企业知识回答。",
        question="合同多久完成初审？",
        category="合同咨询",
        hits=[RetrievalHit(1, "合同规则", "材料齐全后两个工作日完成初审。", 0.9)],
        tool_result="转交法务复核",
    )
    ungrounded = composer.prepare(
        assistant_prompt="只依据企业知识回答。",
        question="未知事项如何处理？",
        category="一般咨询",
        hits=[],
        tool_result=None,
    )

    assert "材料齐全后两个工作日" in grounded["user_prompt"]
    assert "转交法务复核" in grounded["user_prompt"]
    assert "未检索到可用来源" in ungrounded["user_prompt"]


def test_groundedness_gate_accepts_cited_answers_and_flags_ungrounded_ones() -> None:
    gate = GroundednessGate(0.15)
    excerpt = "开票申请需要提供订单号、开票抬头、税号、金额与邮箱；已完成交付确认的订单可在 1 个工作日内安排开票。"
    grounded_answer = (
        "根据企业知识，开票申请需要提供订单号、开票抬头、税号、金额与邮箱；"
        "已完成交付确认的订单可在 1 个工作日内安排开票，请提前准备材料。"
    )
    ungrounded_answer = "这是一个与知识库完全无关的长篇回答，讨论天气、交通和今天午饭吃了什么，没有任何依据。"

    ok_status, _ok_detail, ok_score = gate.check(grounded_answer, [RetrievalHit(1, "开票指引", excerpt, 0.9)])
    bad_status, _bad_detail, bad_score = gate.check(ungrounded_answer, [RetrievalHit(1, "开票指引", excerpt, 0.9)])
    short_status, _short_detail, short_score = gate.check("太短", [RetrievalHit(1, "开票指引", excerpt, 0.9)])
    no_hits_status, _d, _s = gate.check(grounded_answer, [])

    assert ok_status == "completed" and ok_score > 0.15
    assert bad_status == "fallback" and bad_score < 0.15
    assert short_status == "fallback" and short_score == 0.0
    assert no_hits_status == "fallback"


def test_tool_call_parser_rejects_malformed_arguments() -> None:
    calls, parse_failed = parse_openai_tool_calls(
        [
            {
                "id": "call_good_1",
                "type": "function",
                "function": {"name": "order_status_lookup", "arguments": "{}"},
            }
        ]
    )
    assert parse_failed is False
    assert calls == (LLMToolCall(id="call_good_1", name="order_status_lookup", arguments={}),)

    malformed_calls, malformed = parse_openai_tool_calls(
        [
            {
                "id": "call_bad_1",
                "type": "function",
                "function": {"name": "order_status_lookup", "arguments": "[]"},
            }
        ]
    )
    assert malformed_calls == ()
    assert malformed is True


def test_seeded_orders_exist_for_the_lookup_tool(seeded_db: Session) -> None:
    refs = set(seeded_db.scalars(select(Order.order_ref)).all())
    assert {"A-1024", "A-1025", "A-1026", "B-2077", "C-3001"} <= refs


# ---------------------------------------------------------------------------
# Provider-backed intent layer / driver handoff (scripted OpenAI-compatible)
# ---------------------------------------------------------------------------


class ScriptedProviderLLM(OpenAICompatibleClient):
    """Runs the real rewrite/router/gate code paths with scripted completions."""

    def __init__(self, responses: list[Completion]) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []

    async def complete(self, system_prompt: str, user_prompt: str, **_: object) -> Completion:
        self.prompts.append(user_prompt)
        return self.responses.pop(0)

    async def stream_complete(self, system_prompt: str, user_prompt: str, **_: object) -> AsyncIterator[object]:
        completion = await self.complete(system_prompt, user_prompt)
        if completion.text:
            yield LLMStreamTextDelta(completion.text)
        yield LLMStreamCompleted(completion)


def _with_llm_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        agent_service, "settings", replace(app_settings, llm_api_key="test-provider-key")
    )


def test_intent_router_retries_invalid_output_then_accepts(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_llm_key(monkeypatch)
    fake = ScriptedProviderLLM(
        [
            Completion(text="抱歉，我不能输出 JSON。", used_fallback=False),
            Completion(
                text='前置说明 {"route": "complex", "category": "系统故障"} 后缀',
                used_fallback=False,
            ),
        ]
    )
    workflow = AssistantWorkflow(llm_client=fake)

    category, route = asyncio.run(workflow.intent_router.route(workflow, "这服务挂了", None))

    assert (category, route) == ("系统故障", "complex")
    assert "模型路由判定" in workflow.intent_router.last_detail
    assert "上一次输出不是合法 JSON" in fake.prompts[1]


def test_intent_router_falls_back_to_keywords_after_two_invalid_outputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_llm_key(monkeypatch)
    workflow = AssistantWorkflow(
        llm_client=ScriptedProviderLLM(
            [
                Completion(text='{"route": "nonsense", "category": "不存在"}', used_fallback=False),
                Completion(text='{"route": "knowledge", "category": "编造类别"}', used_fallback=False),
            ]
        )
    )

    category, route = asyncio.run(workflow.intent_router.route(workflow, "开发票要什么材料", None))

    assert (category, route) == ("发票办理", "knowledge")
    assert "路由输出两次无效" in workflow.intent_router.last_detail


def test_query_rewriter_retries_overlong_output_and_keeps_identifiers(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_llm_key(monkeypatch)
    fake = ScriptedProviderLLM(
        [
            Completion(text="冗长输出" * 60, used_fallback=False),
            Completion(text="订单 A-1024 的交付进度如何", used_fallback=False),
        ]
    )
    workflow = AssistantWorkflow(llm_client=fake)

    rewritten = asyncio.run(workflow.query_rewriter.rewrite(workflow, "订单 A-1024 进度", (), None))

    assert rewritten == "订单 A-1024 的交付进度如何"
    assert "上一次输出无效" in fake.prompts[1]


def test_llm_tool_loop_replaces_deterministic_driver_for_escalation(
    seeded_db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a live LLM the graph must NOT also run the deterministic driver."""

    _with_llm_key(monkeypatch)
    workflow = AssistantWorkflow(
        llm_client=ScriptedProviderLLM(
            [
                Completion(text="系统出现故障，服务不可用", used_fallback=False),
                Completion(
                    text='{"route": "complex", "category": "系统故障"}', used_fallback=False
                ),
                Completion(
                    text="系统故障处理指引：系统出现故障，服务不可用，需要人工核验并跟进。",
                    used_fallback=False,
                ),
                Completion(
                    text="系统故障处理指引：系统出现故障，服务不可用，需要人工核验并跟进。",
                    used_fallback=False,
                ),
            ]
        )
    )
    result = asyncio.run(workflow.run(seeded_db, "系统又崩溃了"))

    # Exactly one escalation path: the model answered without calling the
    # creation tool, and the deterministic driver stayed out of the way.
    assert _ticket_count(seeded_db) == 3
    tool_trace = next(item for item in result.trace if item.step == "工具调用")
    assert tool_trace.status == "skipped"


def test_groundedness_gate_failure_retries_then_appends_handoff_suggestion(
    seeded_db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_llm_key(monkeypatch)
    ungrounded = "这是一个与知识库毫无关系并且足够长的回答，聊的是天气、交通与午饭吃了什么。"
    workflow = AssistantWorkflow(
        llm_client=ScriptedProviderLLM(
            [
                Completion(text="开票所需材料说明", used_fallback=False),
                Completion(
                    text='{"route": "knowledge", "category": "发票办理"}', used_fallback=False
                ),
                Completion(text=ungrounded, used_fallback=False),
                Completion(text=ungrounded, used_fallback=False),
            ]
        )
    )

    result = asyncio.run(workflow.run(seeded_db, "发票申请需要什么材料？"))

    assert result.answer.endswith("建议：请转人工服务人员核验后继续处理。")
    quality_trace = next(item for item in result.trace if item.step == "回答质检")
    assert quality_trace.status == "fallback"
