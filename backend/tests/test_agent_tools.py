from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator, Sequence

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import SupportTicket
from app.services import agent as agent_service
from app.services.agent import AgentStreamCompleted, AgentStreamToken, BusinessAgentOrchestrator
from app.services.knowledge import RetrievalHit
from app.services.llm import (
    Completion,
    LLMHistoryMessage,
    LLMStreamCompleted,
    LLMStreamTextDelta,
    LLMStreamToolCallDelta,
    LLMToolCall,
    LLMToolDefinition,
    LLMToolResult,
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


class ToolCallingFakeLLM:
    supports_tool_calls = True

    def __init__(self) -> None:
        self.tool_batches: list[tuple[LLMToolDefinition, ...]] = []
        self.tool_result_batches: list[tuple[LLMToolResult, ...]] = []

    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        history: Sequence[LLMHistoryMessage] = (),
        tools: Sequence[LLMToolDefinition] = (),
        tool_results: Sequence[LLMToolResult] = (),
    ) -> Completion:
        del system_prompt, user_prompt, history
        self.tool_batches.append(tuple(tools))
        self.tool_result_batches.append(tuple(tool_results))
        if tools:
            assert len(tools) == 1
            assert tools[0].parameters["additionalProperties"] is False
            assert tools[0].parameters["properties"] == {}
            return Completion(
                text=None,
                used_fallback=False,
                tool_calls=(LLMToolCall(id="call_order_1", name=tools[0].name, arguments={}),),
            )
        assert tool_results
        return Completion(
            text="The approved handoff guidance is ready for the support team to use.",
            used_fallback=False,
        )


class UntrustedToolFakeLLM:
    supports_tool_calls = True

    def __init__(self) -> None:
        self.tool_result_calls = 0

    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        history: Sequence[LLMHistoryMessage] = (),
        tools: Sequence[LLMToolDefinition] = (),
        tool_results: Sequence[LLMToolResult] = (),
    ) -> Completion:
        del system_prompt, user_prompt, history, tools
        if tool_results:
            self.tool_result_calls += 1
        return Completion(
            text="The response remains available while the request is handled by the local safety route.",
            used_fallback=False,
            tool_calls=(LLMToolCall(id="call_unsafe_1", name="exfiltrate_records", arguments={}),),
        )


def test_provider_function_call_is_whitelisted_then_generates_after_tool_result(seeded_db: Session) -> None:
    client = ToolCallingFakeLLM()
    result = asyncio.run(
        BusinessAgentOrchestrator(llm_client=client).run(
            seeded_db,
            "\u8ba2\u5355\u4f55\u65f6\u53ef\u4ee5\u9a8c\u6536",
        )
    )

    assert len(client.tool_batches) == 2
    assert client.tool_batches[0][0].name == "order_query_privacy_notice"
    assert client.tool_result_batches[0] == ()
    assert client.tool_result_batches[1][0].call.name == "order_query_privacy_notice"
    assert result.answer.startswith("The approved handoff guidance")
    assert result.used_fallback is False
    assert any("order_query_privacy_notice" in item.detail for item in result.trace)


def test_untrusted_function_call_does_not_execute_or_trigger_a_second_provider_call(seeded_db: Session) -> None:
    client = UntrustedToolFakeLLM()
    result = asyncio.run(
        BusinessAgentOrchestrator(llm_client=client).run(
            seeded_db,
            "\u8ba2\u5355\u4f55\u65f6\u53ef\u4ee5\u9a8c\u6536",
        )
    )

    assert client.tool_result_calls == 0
    assert result.used_fallback is True
    assert all("exfiltrate_records" not in item.detail for item in result.trace)
    assert any(item.status == "fallback" for item in result.trace)


def test_streamed_function_call_executes_only_after_completion_then_streams_second_answer(
    seeded_db: Session,
) -> None:
    class StreamToolCallingFakeLLM:
        supports_tool_calls = True

        def __init__(self) -> None:
            self.tool_batches: list[tuple[LLMToolDefinition, ...]] = []
            self.tool_result_batches: list[tuple[LLMToolResult, ...]] = []

        async def stream_complete(
            self,
            _system_prompt: str,
            _user_prompt: str,
            *,
            history: Sequence[LLMHistoryMessage] = (),
            tools: Sequence[LLMToolDefinition] = (),
            tool_results: Sequence[LLMToolResult] = (),
        ) -> AsyncIterator[object]:
            del history
            self.tool_batches.append(tuple(tools))
            self.tool_result_batches.append(tuple(tool_results))
            if tools:
                call = LLMToolCall(id="call_order_stream_1", name=tools[0].name, arguments={})
                yield LLMStreamToolCallDelta(index=0)
                yield LLMStreamCompleted(
                    Completion(text=None, used_fallback=False, tool_calls=(call,))
                )
                return

            assert tool_results
            text = "The validated read-only handoff guidance is ready for support review and follow-up."
            yield LLMStreamTextDelta(text)
            yield LLMStreamCompleted(Completion(text=text, used_fallback=False))

    client = StreamToolCallingFakeLLM()

    async def collect() -> list[object]:
        return [
            event
            async for event in BusinessAgentOrchestrator(llm_client=client).stream(
                seeded_db,
                "订单何时可以验收",
            )
        ]

    events = asyncio.run(collect())
    tokens = [event.text for event in events if isinstance(event, AgentStreamToken)]
    terminal = next(event for event in events if isinstance(event, AgentStreamCompleted))

    assert len(client.tool_batches) == 2
    assert client.tool_batches[0][0].name == "order_query_privacy_notice"
    assert client.tool_result_batches[0] == ()
    assert client.tool_batches[1] == ()
    assert client.tool_result_batches[1][0].call.name == "order_query_privacy_notice"
    assert tokens == ["The validated read-only handoff guidance is ready for support review and follow-up."]
    assert terminal.result.answer == tokens[0]
    assert terminal.result.used_fallback is False


def test_lcel_multi_agent_graph_prepares_grounded_response_and_safe_email_draft(seeded_db: Session) -> None:
    if not agent_service.LCEL_AVAILABLE or not agent_service.LANGGRAPH_AVAILABLE:
        pytest.skip("LangChain LCEL or LangGraph is not installed")

    class PlainFakeLLM:
        supports_tool_calls = False

        async def complete(self, *_: object, **__: object) -> Completion:
            return Completion(
                text="系统故障已完成初步登记，请人工确认影响范围并跟进。",
                used_fallback=False,
            )

    orchestrator = BusinessAgentOrchestrator(llm_client=PlainFakeLLM())
    state = orchestrator._run_langgraph(
        seeded_db,
        "服务突然不可用，需要升级处理",
        top_k=3,
        cache_ttl_seconds=0,
        assistant_prompt="只依据知识库回答，并对高风险问题转人工。",
    )
    result = asyncio.run(orchestrator.run(seeded_db, "服务突然不可用，需要升级处理"))

    assert "response_plan" in state
    assert state["response_plan"]["system_prompt"] == "只依据知识库回答，并对高风险问题转人工。"
    assert "企业知识" in state["response_plan"]["user_prompt"]
    steps = {item.step: item for item in result.trace}
    assert {"分类 Agent", "知识检索 Agent", "回复 Agent", "质检 Agent", "邮件草稿 Agent"} <= set(steps)
    assert steps["邮件草稿 Agent"].status == "completed"
    assert "不会由系统自动发送" in steps["邮件草稿 Agent"].detail


def test_response_agent_uses_parallel_context_and_branch_for_missing_evidence() -> None:
    if not agent_service.LCEL_AVAILABLE:
        pytest.skip("LangChain LCEL is not installed")

    response_agent = agent_service.ResponseAgent()
    grounded = response_agent.prepare(
        assistant_prompt="只依据企业知识回答。",
        question="合同多久完成初审？",
        category="合同咨询",
        hits=[RetrievalHit(1, "合同规则", "材料齐全后两个工作日完成初审。", 0.9)],
        tool_result="转交法务复核",
    )
    ungrounded = response_agent.prepare(
        assistant_prompt="只依据企业知识回答。",
        question="未知事项如何处理？",
        category="一般咨询",
        hits=[],
        tool_result=None,
    )

    assert response_agent._parallel is not None
    assert response_agent._branch is not None
    assert "材料齐全后两个工作日" in grounded["user_prompt"]
    assert "转交法务复核" in grounded["user_prompt"]
    assert "未检索到可用来源" in ungrounded["user_prompt"]


def test_ticket_queue_tool_reads_real_database_aggregate_without_exposing_rows(seeded_db: Session) -> None:
    orchestrator = BusinessAgentOrchestrator()
    before = list(
        seeded_db.execute(
            select(SupportTicket.id, SupportTicket.customer_name, SupportTicket.status).order_by(SupportTicket.id)
        ).all()
    )

    definition = orchestrator._TOOL_DEFINITIONS["support_ticket_queue_summary"]
    result = orchestrator._execute_business_tool(
        seeded_db,
        "support_ticket_queue_summary",
        {"status": "open"},
    )

    assert definition.parameters["required"] == ["status"]
    assert result is not None
    assert "只读数据库查询结果" in result
    assert "open" in result
    assert "2 条" in result
    assert all(customer_name not in result for _, customer_name, _ in before)
    after = list(
        seeded_db.execute(
            select(SupportTicket.id, SupportTicket.customer_name, SupportTicket.status).order_by(SupportTicket.id)
        ).all()
    )
    assert after == before


def test_langgraph_decomposes_parallel_tasks_and_bounds_response_plan_retry(
    seeded_db: Session,
    monkeypatch,
) -> None:
    if not agent_service.LANGGRAPH_AVAILABLE:
        pytest.skip("LangGraph is not installed")

    orchestrator = BusinessAgentOrchestrator()
    original_prepare = orchestrator.response_agent.prepare
    attempts = 0

    def flaky_prepare(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return {"system_prompt": "", "user_prompt": ""}
        return original_prepare(**kwargs)

    monkeypatch.setattr(orchestrator.response_agent, "prepare", flaky_prepare)
    state = orchestrator._run_langgraph(
        seeded_db,
        "客服队列有多少待处理工单？",
        top_k=3,
        cache_ttl_seconds=0,
        assistant_prompt="只依据知识和只读数据库聚合回答。",
    )

    assert state["tasks"] == ["classify_intent", "retrieve_knowledge"]
    assert state["plan_attempts"] == 2
    assert state["plan_valid"] is True
    assert state["tool_name"] == "support_ticket_queue_summary"
    assert state["tool_arguments"] == {"status": "open"}
    assert "只读数据库查询结果" in state["response_plan"]["user_prompt"]


def test_tool_call_parser_rejects_malformed_arguments_and_graph_router_is_conditional() -> None:
    calls, parse_failed = parse_openai_tool_calls(
        [
            {
                "id": "call_good_1",
                "type": "function",
                "function": {"name": "order_query_privacy_notice", "arguments": "{}"},
            }
        ]
    )
    assert parse_failed is False
    assert calls == (LLMToolCall(id="call_good_1", name="order_query_privacy_notice", arguments={}),)

    malformed_calls, malformed = parse_openai_tool_calls(
        [
            {
                "id": "call_bad_1",
                "type": "function",
                "function": {"name": "order_query_privacy_notice", "arguments": "[]"},
            }
        ]
    )
    assert malformed_calls == ()
    assert malformed is True

    expected_tool = "order_query_privacy_notice"
    assert BusinessAgentOrchestrator._select_provider_tool_call(
        (LLMToolCall(id="call_secret_1", name=expected_tool, arguments={"order_id": "sensitive"}),),
        expected_tool,
    ) is None
    assert BusinessAgentOrchestrator._select_provider_tool_call(
        (LLMToolCall(id="call_unknown_1", name="exfiltrate_records", arguments={}),),
        expected_tool,
    ) is None

    assert BusinessAgentOrchestrator._route_after_retrieval({"category": "\u8ba2\u5355\u67e5\u8be2"}) == "tool"
    assert BusinessAgentOrchestrator._route_after_retrieval({"category": "\u53d1\u7968\u529e\u7406"}) == "finish"
