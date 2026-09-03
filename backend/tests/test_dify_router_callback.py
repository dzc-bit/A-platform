from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app import api as api_module
from app.routers import shared as api_shared
from app.schemas import AgentTrace, Artifact
from app.services.agent import AgentResult, AssistantWorkflow
from app.services.dify import DifyMediaResult, DifyWorkflowResult

from .conftest import login


CALLBACK_SECRET = "test-dify-callback-secret"


def _callback_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "query": "查询待处理事项",
        "context": [],
        "conversation_id": None,
        "user_id": "user-7",
        "route": "complex",
        "request_id": "request-1",
        "route_depth": 1,
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize("provided_secret", [None, "wrong-secret"])
def test_langgraph_callback_rejects_missing_or_wrong_secret(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    provided_secret: str | None,
) -> None:
    monkeypatch.setattr(
        api_shared,
        "settings",
        replace(api_shared.settings, dify_callback_secret=CALLBACK_SECRET),
    )

    async def must_not_run(*_args: object, **_kwargs: object) -> AgentResult:
        raise AssertionError("unauthenticated callback reached the orchestrator")

    monkeypatch.setattr(api_shared.workflow, "run_callback", must_not_run)
    headers = {"X-Dify-Callback-Secret": provided_secret} if provided_secret else {}

    response = client.post(
        "/api/v1/tools/langgraph/run",
        headers=headers,
        json=_callback_payload(),
    )

    assert response.status_code == 401


def test_langgraph_callback_accepts_correct_secret(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        api_shared,
        "settings",
        replace(api_shared.settings, dify_callback_secret=CALLBACK_SECRET),
    )
    received: dict[str, object] = {}

    async def fake_run_callback(db: object, query: str, **kwargs: object) -> AgentResult:
        received.update({"db": db, "query": query, **kwargs})
        return AgentResult(
            answer="结构化回调结果",
            citations=[],
            trace=[AgentTrace(step="LangGraph", status="completed", detail="ok")],
            used_fallback=False,
            category="复杂任务",
            artifacts=[],
        )

    monkeypatch.setattr(api_shared.workflow, "run_callback", fake_run_callback)
    response = client.post(
        "/api/v1/tools/langgraph/run",
        headers={"X-Dify-Callback-Secret": CALLBACK_SECRET},
        json=_callback_payload(
            query="汇总本周工单",
            context=[{"role": "assistant", "content": "上一轮结果"}],
        ),
    )

    assert response.status_code == 200
    assert response.json() == {
        "answer": "结构化回调结果",
        "category": "复杂任务",
        "citations": [],
        "trace": [{"step": "LangGraph", "status": "completed", "detail": "ok"}],
        "artifacts": [],
        "need_clarification": False,
        "used_fallback": False,
    }
    assert received["query"] == "汇总本周工单"
    assert received["context"] == [{"role": "assistant", "content": "上一轮结果"}]
    assert received["conversation_id"] is None
    assert received["user_id"] == "user-7"
    assert received["route"] == "complex"
    assert received["media_intent"] is None


@pytest.mark.parametrize("route_depth", [2, 99])
def test_langgraph_callback_rejects_recursive_route_depth(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    route_depth: int,
) -> None:
    monkeypatch.setattr(
        api_shared,
        "settings",
        replace(api_shared.settings, dify_callback_secret=CALLBACK_SECRET),
    )

    async def must_not_run(*_args: object, **_kwargs: object) -> AgentResult:
        raise AssertionError("recursive callback reached the orchestrator")

    monkeypatch.setattr(api_shared.workflow, "run_callback", must_not_run)
    response = client.post(
        "/api/v1/tools/langgraph/run",
        headers={"X-Dify-Callback-Secret": CALLBACK_SECRET},
        json=_callback_payload(route_depth=route_depth),
    )

    assert response.status_code == 400


class RecordingMediaGateway:
    def __init__(self) -> None:
        self.tts_calls: list[tuple[str, str, str]] = []
        self.image_calls: list[tuple[str, str, str]] = []

    async def run_text_to_speech(self, text: str, voice: str, user: str) -> DifyMediaResult:
        self.tts_calls.append((text, voice, user))
        return DifyMediaResult(
            kind="audio",
            output={},
            media_url="https://media.example/generated.wav",
            data_url=None,
            content_type="audio/wav",
            byte_size=128,
            mode="remote",
            degraded=False,
            detail="ok",
            status_code=200,
        )

    async def run_text_to_image(self, prompt: str, size: str, user: str) -> DifyMediaResult:
        self.image_calls.append((prompt, size, user))
        return DifyMediaResult(
            kind="image",
            output={},
            media_url="https://media.example/generated.png",
            data_url=None,
            content_type="image/png",
            byte_size=256,
            mode="remote",
            degraded=False,
            detail="ok",
            status_code=200,
        )


def test_media_callback_routes_tts_and_image_to_dify_tools() -> None:
    gateway = RecordingMediaGateway()
    orchestrator = AssistantWorkflow(dify_gateway=gateway)  # type: ignore[arg-type]

    tts_result = asyncio.run(
        orchestrator.run_callback(
            None,  # type: ignore[arg-type]
            "帮我把欢迎使用转成语音",
            user_id="user-8",
            route="media",
        )
    )
    image_result = asyncio.run(
        orchestrator.run_callback(
            None,  # type: ignore[arg-type]
            "请生成一张月球基地图片",
            user_id="user-9",
            route="media",
        )
    )

    assert gateway.tts_calls == [("欢迎使用", "Cherry", "user-8")]
    assert gateway.image_calls == [("月球基地图片", "1024x1024", "user-9")]
    assert [artifact.kind for artifact in tts_result.artifacts] == ["audio"]
    assert [artifact.kind for artifact in image_result.artifacts] == ["image"]


def test_tts_callback_resolves_the_latest_assistant_reply() -> None:
    gateway = RecordingMediaGateway()
    orchestrator = AssistantWorkflow(dify_gateway=gateway)  # type: ignore[arg-type]

    result = asyncio.run(
        orchestrator.run_callback(
            None,  # type: ignore[arg-type]
            "朗读上一条回复",
            context=[
                {"role": "assistant", "content": "较早的回复"},
                {"role": "user", "content": "继续说明"},
                {"role": "assistant", "content": "这是最新的助手回复"},
            ],
            user_id="user-10",
            route="media",
        )
    )

    assert gateway.tts_calls == [("这是最新的助手回复", "Cherry", "user-10")]
    assert result.used_fallback is False
    assert [artifact.kind for artifact in result.artifacts] == ["audio"]


def test_media_failure_never_returns_a_fake_artifact() -> None:
    class UnavailableGateway:
        async def run_text_to_speech(self, *_args: object) -> DifyMediaResult:
            return DifyMediaResult(
                kind="audio",
                output=None,
                media_url=None,
                data_url=None,
                content_type=None,
                byte_size=None,
                mode="unavailable",
                degraded=True,
                detail="upstream unavailable",
                status_code=503,
            )

    orchestrator = AssistantWorkflow(dify_gateway=UnavailableGateway())  # type: ignore[arg-type]
    result = asyncio.run(
        orchestrator.run_callback(
            None,  # type: ignore[arg-type]
            "把这段内容转成语音",
            user_id="user-11",
            route="media",
        )
    )

    assert result.artifacts == []
    assert result.used_fallback is True
    assert "暂不可用" in result.answer


def test_tts_tool_normalizes_default_voice_to_cherry() -> None:
    gateway = RecordingMediaGateway()
    orchestrator = AssistantWorkflow(dify_gateway=gateway)  # type: ignore[arg-type]

    for arguments in (
        {"text": "已审核答复"},
        {"text": "已审核答复", "voice": ""},
        {"text": "已审核答复", "voice": " DeFaUlT "},
        {"text": "已审核答复", "voice": "Serena"},
    ):
        _, artifacts = asyncio.run(
            orchestrator._execute_media_tool("dify_text_to_speech", arguments, "user-12")
        )
        assert [artifact.kind for artifact in artifacts] == ["audio"]

    assert [voice for _, voice, _ in gateway.tts_calls] == [
        "Cherry",
        "Cherry",
        "Cherry",
        "Serena",
    ]


def test_tts_workflow_prepare_text_normalizes_default_voice_to_cherry() -> None:
    workflow_path = Path(__file__).resolve().parents[2] / "dify" / "text-to-speech-workflow.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    prepare_node = next(
        node for node in workflow["workflow"]["graph"]["nodes"] if node["id"] == "prepare_text"
    )
    namespace: dict[str, object] = {}
    exec(prepare_node["data"]["code"], namespace)
    prepare_text = namespace["main"]
    assert callable(prepare_text)

    assert prepare_text(" 已审核   答复 ", None) == {"text": "已审核 答复", "voice": "Cherry"}
    assert prepare_text("已审核答复", "") == {"text": "已审核答复", "voice": "Cherry"}
    assert prepare_text("已审核答复", " DEFAULT ") == {"text": "已审核答复", "voice": "Cherry"}
    assert prepare_text("已审核答复", "Serena") == {"text": "已审核答复", "voice": "Serena"}


def test_invalid_media_arguments_do_not_call_dify() -> None:
    gateway = RecordingMediaGateway()
    orchestrator = AssistantWorkflow(dify_gateway=gateway)  # type: ignore[arg-type]

    _, artifacts = asyncio.run(
        orchestrator._execute_media_tool(
            "dify_text_to_image",
            {"prompt": "产品海报", "size": "invalid-size"},
            "user-12",
        )
    )
    _, invalid_type_artifacts = asyncio.run(
        orchestrator._execute_media_tool(
            "dify_text_to_image",
            {"prompt": 123, "size": "1024x1024"},
            "user-12",
        )
    )
    _, invalid_voice_artifacts = asyncio.run(
        orchestrator._execute_media_tool(
            "dify_text_to_speech",
            {"text": "已审核答复", "voice": 123},
            "user-12",
        )
    )

    assert artifacts == []
    assert invalid_type_artifacts == []
    assert invalid_voice_artifacts == []
    assert gateway.image_calls == []
    assert gateway.tts_calls == []


def test_router_failure_falls_back_to_local_orchestrator(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        api_shared,
        "settings",
        replace(
            api_shared.settings,
            dify_api_url="https://dify.example",
            dify_router_api_key="router-key",
        ),
    )
    calls: list[tuple[str, str]] = []

    async def unavailable_router(query: str, user: str, **_kwargs: object) -> DifyWorkflowResult:
        calls.append(("router", query))
        return DifyWorkflowResult(
            answer=None,
            mode="local_fallback",
            degraded=True,
            detail="router unavailable",
            status_code=502,
        )

    async def local_run(
        _db: object,
        question: str,
        **_kwargs: object,
    ) -> AgentResult:
        calls.append(("local", question))
        return AgentResult(
            answer="本地 LangGraph 回答",
            citations=[],
            trace=[AgentTrace(step="本地 LangGraph", status="completed", detail="ok")],
            used_fallback=False,
            category="一般咨询",
            artifacts=[Artifact(kind="image", media_url="https://media.example/local.png")],
        )

    monkeypatch.setattr(api_shared.dify_gateway, "run_router_workflow", unavailable_router)
    monkeypatch.setattr(api_shared.workflow, "run", local_run)
    response = client.post(
        "/api/v1/assistant/chat",
        headers=login(client),
        json={"message": "普通业务问题", "mode": "assistant"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["answer"] == "本地 LangGraph 回答"
    assert payload["artifacts"] == [
        {
            "kind": "image",
            "media_url": "https://media.example/local.png",
            "data_url": None,
            "content_type": None,
            "byte_size": None,
        }
    ]
    assert calls == [("router", "普通业务问题"), ("local", "普通业务问题")]


def test_router_success_parses_stringified_artifacts_and_sends_context(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        api_shared,
        "settings",
        replace(
            api_shared.settings,
            dify_api_url="https://dify.example",
            dify_router_api_key="router-key",
        ),
    )
    received: dict[str, object] = {}

    async def successful_router(query: str, user: str, **kwargs: object) -> DifyWorkflowResult:
        received.update({"query": query, "user": user, **kwargs})
        return DifyWorkflowResult(
            answer=None,
            mode="remote",
            degraded=False,
            detail="ok",
            outputs={
                "answer": "已生成语音",
                "category": "语音生成",
                "citations": [{
                    "title": "Dify 制度文档",
                    "content": "引用正文",
                    "metadata": {"document_id": "doc-uuid-1", "score": 0.91},
                }],
                "trace": json.dumps([
                    {"step": "媒体工具 Agent", "status": "completed", "detail": "ok"}
                ], ensure_ascii=False),
                "artifacts": json.dumps([
                    {
                        "kind": "audio",
                        "media_url": "https://media.example/generated.wav",
                        "content_type": "audio/wav",
                        "byte_size": 128,
                    }
                ]),
            },
            status_code=200,
        )

    monkeypatch.setattr(api_shared.dify_gateway, "run_router_workflow", successful_router)
    response = client.post(
        "/api/v1/assistant/chat",
        headers=login(client),
        json={"message": "朗读这段内容", "mode": "assistant"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["artifacts"][0]["kind"] == "audio"
    assert payload["citations"] == [{
        "document_id": "doc-uuid-1",
        "title": "Dify 制度文档",
        "excerpt": "引用正文",
        "score": 0.91,
    }]
    assert received["context"] == [{"role": "user", "content": "朗读这段内容"}]
    assert received["conversation_id"] == payload["conversation_id"]
    assert received["request_id"]


def test_media_artifact_survives_conversation_history_reload(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        api_shared,
        "settings",
        replace(
            api_shared.settings,
            dify_api_url="https://dify.example",
            dify_router_api_key="router-key",
        ),
    )
    artifact = {
        "kind": "audio",
        "media_url": "https://media.example/generated.wav",
        "data_url": None,
        "content_type": "audio/wav",
        "byte_size": 128,
    }
    published: list[dict[str, object]] = []

    async def capture_event(event: dict[str, object]) -> None:
        published.append(event)

    async def successful_router(*_args: object, **_kwargs: object) -> DifyWorkflowResult:
        return DifyWorkflowResult(
            answer=None,
            mode="remote",
            degraded=False,
            detail="ok",
            outputs={
                "c_answer": "已生成语音",
                "c_category": "语音生成",
                "c_citations": "[]",
                "c_trace": "[]",
                "c_artifacts": json.dumps([artifact], ensure_ascii=False),
                "c_need_clarification": False,
                "c_used_fallback": False,
            },
            status_code=200,
        )

    monkeypatch.setattr(api_shared.dify_gateway, "run_router_workflow", successful_router)
    monkeypatch.setattr(api_shared.ticket_event_broker, "publish", capture_event)
    headers = login(client)
    chat_response = client.post(
        "/api/v1/assistant/chat",
        headers=headers,
        json={"message": "朗读这段内容", "mode": "assistant"},
    )
    assert chat_response.status_code == 200
    conversation_id = chat_response.json()["conversation_id"]

    history_response = client.get(
        f"/api/v1/assistant/conversations/{conversation_id}/messages",
        headers=headers,
    )

    assert history_response.status_code == 200
    assistant_message = next(
        message for message in reversed(history_response.json()) if message["role"] == "assistant"
    )
    assert assistant_message["artifacts"] == [artifact]
    event_message = published[-1]["message"]
    assert isinstance(event_message, dict)
    assert event_message["artifacts"] == [artifact]


@pytest.mark.parametrize(
    ("prefix", "answer", "category", "used_fallback", "artifacts", "expected_kind"),
    [
        ("b_", "复杂任务结果", "B", True, "[]", None),
        (
            "c_",
            "已生成图片",
            "C",
            False,
            json.dumps(
                [{"kind": "image", "media_url": "https://media.example/generated.png"}],
                ensure_ascii=False,
            ),
            "image",
        ),
    ],
)
def test_router_success_parses_branch_prefixed_outputs(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    prefix: str,
    answer: str,
    category: str,
    used_fallback: bool,
    artifacts: str,
    expected_kind: str | None,
) -> None:
    monkeypatch.setattr(
        api_shared,
        "settings",
        replace(
            api_shared.settings,
            dify_api_url="https://dify.example",
            dify_router_api_key="router-key",
        ),
    )
    prefixed_outputs: dict[str, object] = {
        f"{prefix}answer": answer,
        f"{prefix}citations": "[]",
        f"{prefix}trace": json.dumps(
            [{"step": "Dify 分支", "status": "completed", "detail": category}],
            ensure_ascii=False,
        ),
        f"{prefix}artifacts": artifacts,
        f"{prefix}need_clarification": category == "B",
        f"{prefix}category": category,
        f"{prefix}used_fallback": used_fallback,
    }

    normalized = api_module._normalize_dify_router_outputs(prefixed_outputs)
    for name in (
        "answer",
        "citations",
        "trace",
        "artifacts",
        "need_clarification",
        "category",
        "used_fallback",
    ):
        assert normalized[name] == prefixed_outputs[f"{prefix}{name}"]

    async def successful_router(*_args: object, **_kwargs: object) -> DifyWorkflowResult:
        return DifyWorkflowResult(
            answer=None,
            mode="remote",
            degraded=False,
            detail="ok",
            outputs=prefixed_outputs,
            status_code=200,
        )

    monkeypatch.setattr(api_shared.dify_gateway, "run_router_workflow", successful_router)
    response = client.post(
        "/api/v1/assistant/chat",
        headers=login(client),
        json={"message": f"执行 {category} 分支", "mode": "assistant"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["answer"] == answer
    assert payload["used_fallback"] is used_fallback
    assert payload["trace"][1]["detail"] == category
    assert ([item["kind"] for item in payload["artifacts"]] or [None]) == [expected_kind]


def test_stream_endpoint_uses_router_when_configured(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        api_shared,
        "settings",
        replace(
            api_shared.settings,
            dify_api_url="https://dify.example",
            dify_router_api_key="router-key",
        ),
    )
    calls: list[str] = []

    async def successful_router(query: str, user: str, **_kwargs: object) -> DifyWorkflowResult:
        calls.append(query)
        return DifyWorkflowResult(
            answer=None,
            mode="remote",
            degraded=False,
            detail="ok",
            outputs={"answer": "路由流式结果", "citations": "[]", "trace": "[]", "artifacts": "[]"},
            status_code=200,
        )

    async def must_not_stream(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("configured router path called the local streaming orchestrator")

    monkeypatch.setattr(api_shared.dify_gateway, "run_router_workflow", successful_router)
    monkeypatch.setattr(api_shared.workflow, "stream", must_not_stream)
    response = client.post(
        "/api/v1/assistant/chat/stream",
        headers=login(client),
        json={"message": "走路由工作流", "mode": "assistant"},
    )

    assert response.status_code == 200
    assert "event: done" in response.text
    assert "路由流式结果" in response.text
    assert calls == ["走路由工作流"]
