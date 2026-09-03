from __future__ import annotations

import io
import zipfile
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from .conftest import login
from app import api as api_module
from app.routers import shared as api_shared
from app.database import SessionLocal
from app.models import Conversation, KnowledgeChunk, Message, SupportTicket, User
from app.services.agent import AssistantWorkflow
from app.services.dify import DifyWorkflowResult
from app.services.vision import VisionAnalysisResult


def _docx_bytes(text: str) -> bytes:
    document_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body>
</w:document>'''.encode("utf-8")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types />")
        archive.writestr("word/document.xml", document_xml)
    return buffer.getvalue()


def _pdf_bytes(text: str) -> bytes:
    escaped_text = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 12 Tf 72 720 Td ({escaped_text}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    payload = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for index, object_body in enumerate(objects, start=1):
        offsets.append(len(payload))
        payload.extend(f"{index} 0 obj\n".encode("ascii"))
        payload.extend(object_body)
        payload.extend(b"\nendobj\n")
    xref_offset = len(payload)
    payload.extend(b"xref\n0 6\n0000000000 65535 f \n")
    for offset in offsets:
        payload.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    payload.extend(f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii"))
    return bytes(payload)


def test_offline_agent_evaluation_set_meets_acceptance_threshold() -> None:
    cases = [
        ("系统中断导致客户无法使用", "系统故障"),
        ("合同条款如何变更", "合同咨询"),
        ("开票需要哪些资料", "发票办理"),
        ("订单何时可以验收", "订单查询"),
        ("账号密码无法登录", "账户访问"),
        ("付款金额需要核对", "付款咨询"),
    ]
    agent = AssistantWorkflow()
    accuracy = sum(agent.classify(question) == expected for question, expected in cases) / len(cases)
    assert accuracy >= 0.8


def test_health_reports_degraded_but_ready_service(client: TestClient) -> None:
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["provider"] == "local_demo"
    assert payload["cache"]["mode"] in {"memory", "memory_fallback", "redis"}
    assert payload["security"]["token_secret"]["using_demo_default"] is False
    assert "test-only-secret" not in response.text


def test_health_token_secret_warning_identifies_known_demo_defaults() -> None:
    status = api_module._token_secret_security("change-this-before-production")

    assert status["status"] == "warning"
    assert status["using_demo_default"] is True
    assert "TOKEN_SECRET" in str(status["warning"])


def test_enterprise_user_can_register(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/register",
        json={"email": "new-enterprise@example.test", "password": "StrongPass123!", "display_name": "新企业用户"},
    )

    assert response.status_code == 201, response.text
    assert response.json()["user"]["role"] == "enterprise_user"
    assert response.json()["access_token"]


def test_user_preferences_are_persisted_and_scoped(client: TestClient) -> None:
    enterprise_headers = login(client)
    defaults = client.get("/api/v1/users/me/preferences", headers=enterprise_headers)
    assert defaults.status_code == 200, defaults.text
    assert defaults.json() == {
        "response_style": "balanced",
        "preferred_language": "zh-CN",
        "auto_play_voice": False,
    }

    updated = client.put(
        "/api/v1/users/me/preferences",
        headers=enterprise_headers,
        json={
            "response_style": "detailed",
            "preferred_language": "en-US",
            "auto_play_voice": True,
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["response_style"] == "detailed"
    assert updated.json()["preferred_language"] == "en-US"
    assert updated.json()["auto_play_voice"] is True

    support_headers = login(client, "support@neusoft.local")
    support_preferences = client.get("/api/v1/users/me/preferences", headers=support_headers)
    assert support_preferences.status_code == 200
    assert support_preferences.json()["response_style"] == "balanced"
    assert support_preferences.json()["preferred_language"] == "zh-CN"
    assert support_preferences.json()["auto_play_voice"] is False


def test_seeded_user_can_chat_with_retrieval_citations(client: TestClient) -> None:
    headers = login(client)

    response = client.post(
        "/api/v1/assistant/chat",
        headers=headers,
        json={"message": "开票申请需要准备什么材料？", "mode": "knowledge"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["conversation_id"] > 0
    assert payload["citations"]
    assert "发票" in payload["answer"]
    assert {step["step"] for step in payload["trace"]} >= {"意图路由", "知识检索", "回答质检"}


def test_streaming_chat_emits_trace_token_and_done_events(client: TestClient) -> None:
    headers = login(client)

    response = client.post(
        "/api/v1/assistant/chat/stream",
        headers=headers,
        json={"message": "系统故障应该如何升级？"},
    )

    assert response.status_code == 200
    assert "event: trace" in response.text
    assert "event: token" in response.text
    assert "event: done" in response.text
    assert response.text.index("请求调度") < response.text.index("意图路由")


def test_final_answer_cache_reuses_identical_context_for_json_and_stream(client: TestClient, monkeypatch) -> None:
    original_run = api_shared.workflow.run
    original_stream = api_shared.workflow.stream
    calls = {"run": 0, "stream": 0}

    async def counting_run(*args, **kwargs):
        calls["run"] += 1
        return await original_run(*args, **kwargs)

    async def counting_stream(*args, **kwargs):
        calls["stream"] += 1
        async for event in original_stream(*args, **kwargs):
            yield event

    monkeypatch.setattr(api_shared.workflow, "run", counting_run)
    monkeypatch.setattr(api_shared.workflow, "stream", counting_stream)
    headers = login(client)
    payload = {"message": "开票申请需要准备什么材料？", "mode": "knowledge"}

    first = client.post("/api/v1/assistant/chat", headers=headers, json=payload)
    second = client.post("/api/v1/assistant/chat", headers=headers, json=payload)
    streamed = client.post("/api/v1/assistant/chat/stream", headers=headers, json=payload)

    assert first.status_code == second.status_code == streamed.status_code == 200
    assert calls == {"run": 1, "stream": 0}
    assert any(item["step"] == "最终回答缓存" for item in second.json()["trace"])
    assert '"origin": "cache"' in streamed.text
    assert first.json()["answer"] == second.json()["answer"]

    preference_change = client.put(
        "/api/v1/users/me/preferences",
        headers=headers,
        json={
            "response_style": "concise",
            "preferred_language": "zh-CN",
            "auto_play_voice": False,
        },
    )
    assert preference_change.status_code == 200
    after_preference_change = client.post("/api/v1/assistant/chat", headers=headers, json=payload)
    assert after_preference_change.status_code == 200
    assert calls["run"] == 2

    support_headers = login(client, "support@neusoft.local")
    support_response = client.post("/api/v1/assistant/chat", headers=support_headers, json=payload)
    assert support_response.status_code == 200
    assert calls["run"] == 3


def test_final_answer_cache_invalidates_for_same_timestamp_ticket_distribution_changes(
    client: TestClient, monkeypatch
) -> None:
    original_run = api_shared.workflow.run
    calls = {"run": 0}

    async def counting_run(*args, **kwargs):
        calls["run"] += 1
        return await original_run(*args, **kwargs)

    monkeypatch.setattr(api_shared.workflow, "run", counting_run)
    headers = login(client)
    fixed_updated_at = datetime(2026, 1, 1, tzinfo=timezone.utc)

    with SessionLocal() as db:
        tickets = list(db.scalars(select(SupportTicket)).all())
        target = next(
            (ticket for ticket in tickets if ticket.status == "open" and ticket.priority == "normal"),
            None,
        )
        assert target is not None
        target_id = target.id
        for ticket in tickets:
            ticket.updated_at = fixed_updated_at
        db.commit()

    payload = {"message": "目前有哪些工单需要关注？", "mode": "assistant"}
    first = client.post("/api/v1/assistant/chat", headers=headers, json=payload)
    assert first.status_code == 200, first.text
    assert calls["run"] == 1

    with SessionLocal() as db:
        target = db.get(SupportTicket, target_id)
        assert target is not None
        target.priority = "high"
        target.updated_at = fixed_updated_at
        db.commit()

    after_priority_change = client.post("/api/v1/assistant/chat", headers=headers, json=payload)
    assert after_priority_change.status_code == 200, after_priority_change.text
    assert calls["run"] == 2

    with SessionLocal() as db:
        target = db.get(SupportTicket, target_id)
        assert target is not None
        target.status = "resolved"
        target.updated_at = fixed_updated_at
        db.commit()

    after_status_change = client.post("/api/v1/assistant/chat", headers=headers, json=payload)
    assert after_status_change.status_code == 200, after_status_change.text
    assert calls["run"] == 3


def test_image_analysis_accepts_supported_images_without_persisting_bytes(client: TestClient, monkeypatch) -> None:
    headers = login(client)
    observed: dict[str, object] = {}

    async def fake_analyze(image_bytes: bytes, media_type: str, prompt: str, *, model: str | None) -> VisionAnalysisResult:
        observed.update({"image_bytes": image_bytes, "media_type": media_type, "prompt": prompt, "model": model})
        return VisionAnalysisResult(answer="图片中可见一张红色状态提示卡。", used_fallback=False, detail="测试视觉模型")

    monkeypatch.setattr(api_shared.vision_service, "analyze", fake_analyze)
    response = client.post(
        "/api/v1/assistant/image-analysis",
        headers=headers,
        data={"prompt": "识别界面上的状态"},
        files={"file": ("status.png", b"\x89PNG\r\n\x1a\nplaceholder", "image/png")},
    )

    assert response.status_code == 200, response.text
    assert response.json()["used_fallback"] is False
    assert observed["media_type"] == "image/png"
    assert observed["prompt"] == "识别界面上的状态"

    rejected = client.post(
        "/api/v1/assistant/image-analysis",
        headers=headers,
        files={"file": ("unsafe.txt", b"not an image", "text/plain")},
    )
    assert rejected.status_code == 415


def test_support_agent_can_upload_and_search_knowledge(client: TestClient) -> None:
    headers = login(client, "support@neusoft.local")
    upload = client.post(
        "/api/v1/knowledge/upload",
        headers=headers,
        files={"file": ("onboarding.txt", "新员工入职后需要在三个工作日内完成企业权限开通和安全培训。", "text/plain")},
    )

    assert upload.status_code == 201, upload.text
    result = client.post(
        "/api/v1/knowledge/search",
        headers=headers,
        json={"query": "入职权限开通需要多久", "top_k": 3},
    )
    assert result.status_code == 200
    assert any(item["title"] == "onboarding.txt" for item in result.json()["results"])


def test_knowledge_document_management_is_limited_to_support_and_admin(client: TestClient) -> None:
    enterprise_headers = login(client)
    assert client.get("/api/v1/knowledge/documents", headers=enterprise_headers).status_code == 403
    denied_upload = client.post(
        "/api/v1/knowledge/upload",
        headers=enterprise_headers,
        files={"file": ("restricted.txt", "This upload must be rejected for enterprise users.", "text/plain")},
    )
    assert denied_upload.status_code == 403

    support_headers = login(client, "support@neusoft.local")
    assert client.get("/api/v1/knowledge/documents", headers=support_headers).status_code == 200


def test_support_can_edit_reindex_and_delete_knowledge_with_runtime_chunk_settings(client: TestClient) -> None:
    support_headers = login(client, "support@neusoft.local")
    created = client.post(
        "/api/v1/knowledge/documents",
        headers=support_headers,
        json={
            "title": "知识维护测试文档",
            "source": "测试来源",
            "content": "原始知识内容用于验证文档维护和索引更新能力。" * 5,
        },
    )
    assert created.status_code == 201, created.text
    document_id = created.json()["id"]

    updated_content = "修改后的知识内容用于验证运行时分块配置。" * 18
    updated = client.put(
        f"/api/v1/knowledge/documents/{document_id}",
        headers=support_headers,
        json={"title": "已更新的知识文档", "source": "客服维护", "content": updated_content},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["title"] == "已更新的知识文档"
    assert updated.json()["content"] == updated_content
    assert updated.json()["status"] == "ready"

    admin_headers = login(client, "admin@neusoft.local")
    for key, value in (("knowledge_chunk_size", "100"), ("knowledge_chunk_overlap", "20")):
        configured = client.put(
            f"/api/v1/admin/settings/{key}",
            headers=admin_headers,
            json={"value": value, "description": "知识索引测试配置"},
        )
        assert configured.status_code == 200, configured.text

    reindexed = client.post(
        f"/api/v1/knowledge/documents/{document_id}/reindex",
        headers=support_headers,
    )
    assert reindexed.status_code == 200, reindexed.text
    assert reindexed.json()["status"] == "ready"
    assert reindexed.json()["document"]["id"] == document_id
    assert reindexed.json()["indexed_chunks"] >= 3

    with SessionLocal() as db:
        chunk_count = db.scalar(
            select(func.count(KnowledgeChunk.id)).where(KnowledgeChunk.document_id == document_id)
        )
        assert chunk_count == reindexed.json()["indexed_chunks"]

    deleted = client.delete(f"/api/v1/knowledge/documents/{document_id}", headers=support_headers)
    assert deleted.status_code == 204, deleted.text
    assert all(
        document["id"] != document_id
        for document in client.get("/api/v1/knowledge/documents", headers=support_headers).json()
    )
    with SessionLocal() as db:
        assert db.scalar(
            select(func.count(KnowledgeChunk.id)).where(KnowledgeChunk.document_id == document_id)
        ) == 0

    assert client.post(
        f"/api/v1/knowledge/documents/{document_id}/reindex",
        headers=support_headers,
    ).status_code == 404


def test_enterprise_user_cannot_edit_delete_or_reindex_knowledge(client: TestClient) -> None:
    enterprise_headers = login(client)
    document_id = 1
    update_payload = {
        "title": "无权修改的知识文档",
        "source": "越权测试",
        "content": "企业用户不应能够修改知识库中的文档内容。",
    }

    assert client.put(
        f"/api/v1/knowledge/documents/{document_id}",
        headers=enterprise_headers,
        json=update_payload,
    ).status_code == 403
    assert client.delete(
        f"/api/v1/knowledge/documents/{document_id}",
        headers=enterprise_headers,
    ).status_code == 403
    assert client.post(
        f"/api/v1/knowledge/documents/{document_id}/reindex",
        headers=enterprise_headers,
    ).status_code == 403


def test_upload_extracts_csv_docx_and_pdf(client: TestClient) -> None:
    headers = login(client, "support@neusoft.local")
    uploads = [
        (
            "onboarding.csv",
            b"topic,policy\nOnboarding,Employees complete account training within three business days.\n",
            "text/csv",
            "Onboarding",
        ),
        (
            "contract.docx",
            _docx_bytes("Contract approvals require the complete commercial review package."),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "Contract approvals",
        ),
        (
            "handbook.pdf",
            _pdf_bytes("PDF knowledge documents are parsed as searchable plain text."),
            "application/pdf",
            "PDF knowledge documents",
        ),
    ]
    for filename, content, media_type, expected_text in uploads:
        response = client.post(
            "/api/v1/knowledge/upload",
            headers=headers,
            files={"file": (filename, content, media_type)},
        )
        assert response.status_code == 201, response.text
        assert expected_text in response.json()["content"]


def test_upload_rejects_legacy_doc_and_invalid_docx(client: TestClient) -> None:
    headers = login(client, "support@neusoft.local")
    legacy_document = client.post(
        "/api/v1/knowledge/upload",
        headers=headers,
        files={"file": ("legacy.doc", b"Legacy binary document content that must not be parsed.", "application/msword")},
    )
    assert legacy_document.status_code == 415
    assert ".doc" in legacy_document.json()["detail"]

    invalid_docx = client.post(
        "/api/v1/knowledge/upload",
        headers=headers,
        files={"file": ("not-a-docx.docx", b"not a zip archive", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    assert invalid_docx.status_code == 422

    too_large = client.post(
        "/api/v1/knowledge/upload",
        headers=headers,
        files={"file": ("too-large.txt", b"x" * (5 * 1024 * 1024 + 1), "text/plain")},
    )
    assert too_large.status_code == 413


def test_support_cannot_access_or_continue_another_users_conversation(client: TestClient) -> None:
    user_headers = login(client)
    created = client.post(
        "/api/v1/assistant/chat",
        headers=user_headers,
        json={"message": "I need help with an account access issue.", "mode": "assistant"},
    )
    assert created.status_code == 200, created.text
    conversation_id = created.json()["conversation_id"]

    support_headers = login(client, "support@neusoft.local")
    messages = client.get(f"/api/v1/assistant/conversations/{conversation_id}/messages", headers=support_headers)
    assert messages.status_code == 403
    continued = client.post(
        "/api/v1/assistant/chat",
        headers=support_headers,
        json={"message": "Attempt to write into another user's conversation.", "conversation_id": conversation_id, "mode": "assistant"},
    )
    assert continued.status_code == 403


def test_support_ticket_draft_does_not_resolve_ticket(client: TestClient) -> None:
    user_headers = login(client)
    created = client.post(
        "/api/v1/support/tickets",
        headers=user_headers,
        json={"customer_name": "测试客户", "question": "我的账号无法登录，需要怎么处理？", "priority": "high"},
    )
    assert created.status_code == 201, created.text

    support_headers = login(client, "support@neusoft.local")
    drafted = client.patch(
        f"/api/v1/support/tickets/{created.json()['id']}",
        headers=support_headers,
        json={"final_reply": "这是待人工确认的回复草稿。", "status": "in_progress"},
    )

    assert drafted.status_code == 200, drafted.text
    assert drafted.json()["final_reply"] == "这是待人工确认的回复草稿。"
    assert drafted.json()["status"] == "in_progress"


def test_support_ticket_can_be_human_confirmed(client: TestClient) -> None:
    user_headers = login(client)
    created = client.post(
        "/api/v1/support/tickets",
        headers=user_headers,
        json={"customer_name": "测试客户", "question": "我的账号无法登录，需要怎么处理？", "priority": "high"},
    )
    assert created.status_code == 201, created.text

    support_headers = login(client, "support@neusoft.local")
    resolved = client.patch(
        f"/api/v1/support/tickets/{created.json()['id']}",
        headers=support_headers,
        json={"final_reply": "您好，已核验您的信息并安排账户重置。", "status": "resolved"},
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["status"] == "resolved"
    assert resolved.json()["final_reply"].startswith("您好")


def test_ticket_mutations_publish_realtime_events(client: TestClient, monkeypatch) -> None:
    published: list[dict[str, object]] = []

    async def capture(event: dict[str, object]) -> None:
        published.append(event)

    monkeypatch.setattr(api_shared.ticket_event_broker, "publish", capture)
    user_headers = login(client)
    created = client.post(
        "/api/v1/support/tickets",
        headers=user_headers,
        json={"customer_name": "实时事件客户", "question": "系统故障影响演示，需要升级。", "priority": "urgent"},
    )
    assert created.status_code == 201, created.text

    support_headers = login(client, "support@neusoft.local")
    updated = client.patch(
        f"/api/v1/support/tickets/{created.json()['id']}",
        headers=support_headers,
        json={"status": "in_progress", "final_reply": "正在核验的草稿。"},
    )
    assert updated.status_code == 200, updated.text

    assert [event["action"] for event in published] == ["created", "updated"]
    assert published[0]["ticket"]["id"] == created.json()["id"]
    assert published[1]["ticket"]["status"] == "in_progress"
    assert client.get("/api/v1/support/tickets/events").status_code == 401


def test_admin_can_manage_users_and_settings(client: TestClient) -> None:
    admin_headers = login(client, "admin@neusoft.local")
    users = client.get("/api/v1/admin/users", headers=admin_headers)
    assert users.status_code == 200
    assert len(users.json()) == 4

    configured = client.put(
        "/api/v1/admin/settings/retrieval_top_k",
        headers=admin_headers,
        json={"value": "4", "description": "测试时调整"},
    )
    assert configured.status_code == 200
    assert configured.json()["value"] == "4"


def test_admin_cannot_remove_the_last_active_admin(client: TestClient) -> None:
    admin_headers = login(client, "admin@neusoft.local")
    users = client.get("/api/v1/admin/users", headers=admin_headers).json()
    admin = next(user for user in users if user["role"] == "admin" and user["is_active"])

    deactivated = client.patch(
        f"/api/v1/admin/users/{admin['id']}",
        headers=admin_headers,
        json={"role": "admin", "is_active": False},
    )
    assert deactivated.status_code == 409

    demoted = client.patch(
        f"/api/v1/admin/users/{admin['id']}",
        headers=admin_headers,
        json={"role": "enterprise_user", "is_active": True},
    )
    assert demoted.status_code == 409


def test_admin_settings_reject_unknown_keys_and_invalid_values(client: TestClient) -> None:
    admin_headers = login(client, "admin@neusoft.local")
    unsupported_key = client.put(
        "/api/v1/admin/settings/not_a_real_setting",
        headers=admin_headers,
        json={"value": "anything", "description": "invalid setting"},
    )
    assert unsupported_key.status_code == 422

    invalid_top_k = client.put(
        "/api/v1/admin/settings/retrieval_top_k",
        headers=admin_headers,
        json={"value": "99", "description": "outside supported range"},
    )
    assert invalid_top_k.status_code == 422

    normalized_value = client.put(
        "/api/v1/admin/settings/retrieval_top_k",
        headers=admin_headers,
        json={"value": " 4 ", "description": "valid range"},
    )
    assert normalized_value.status_code == 200
    assert normalized_value.json()["value"] == "4"


def test_dashboard_provides_metrics_and_report(client: TestClient) -> None:
    enterprise_headers = login(client)
    denied = client.get("/api/v1/dashboard/overview", headers=enterprise_headers)
    assert denied.status_code == 403

    executive_headers = login(client, "executive@neusoft.local")
    assert client.get("/api/v1/support/tickets", headers=executive_headers).status_code == 403

    admin_headers = login(client, "admin@neusoft.local")
    dashboard = client.get("/api/v1/dashboard/overview", headers=admin_headers)
    assert dashboard.status_code == 200
    overview = dashboard.json()
    assert len(overview["metrics"]) == 4
    metric_labels = {metric["label"] for metric in overview["metrics"]}
    assert "AI 建议质检代理分" in metric_labels
    assert all("满意" not in label for label in metric_labels)
    assert overview["category_distribution"]
    assert overview["satisfaction_trend"] == [
        {"date": datetime.now(timezone.utc).strftime("%m-%d"), "value": 94.0}
    ]
    assert "+12%" not in dashboard.text
    assert "+3.4%" not in dashboard.text
    assert "暂无历史对比" in overview["metrics"][0]["delta"]

    assert client.get("/api/v1/dashboard/overview", headers=executive_headers).status_code == 200

    report = client.get("/api/v1/dashboard/report", headers=admin_headers)
    assert report.status_code == 200
    assert "简报" in report.json()["title"]


def test_dashboard_uses_current_and_previous_seven_day_database_records(client: TestClient) -> None:
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        seeded_tickets = list(db.scalars(select(SupportTicket).order_by(SupportTicket.id)).all())
        for ticket, quality in zip(seeded_tickets, (0.7, 0.8, 0.9), strict=True):
            ticket.created_at = now - timedelta(days=8)
            ticket.quality_score = quality

        enterprise_user = db.scalar(select(User).where(User.email == "enterprise@neusoft.local"))
        assert enterprise_user is not None
        conversation = Conversation(user_id=enterprise_user.id, title="看板窗口测试", mode="assistant")
        db.add(conversation)
        db.flush()
        db.add_all(
            [
                Message(
                    conversation_id=conversation.id,
                    role="user",
                    content="前一周期咨询",
                    created_at=now - timedelta(days=8),
                ),
                Message(
                    conversation_id=conversation.id,
                    role="user",
                    content="当前周期咨询",
                    created_at=now - timedelta(days=1),
                ),
                SupportTicket(
                    customer_name="时间窗口客户",
                    question="当前周期的真实质量记录用于看板聚合。",
                    category="一般咨询",
                    priority="normal",
                    status="resolved",
                    suggested_reply="已记录。",
                    quality_score=1.0,
                    created_at=now - timedelta(days=1),
                ),
            ]
        )
        db.commit()

    headers = login(client, "admin@neusoft.local")
    response = client.get("/api/v1/dashboard/overview", headers=headers)

    assert response.status_code == 200
    overview = response.json()
    assert overview["metrics"][0]["value"] == 6
    assert overview["metrics"][0]["delta"] == "近7日 2 条；较前7日 -2 条"
    assert overview["metrics"][2]["delta"] == "近7日均值 100%；较前7日 +20.0 个百分点"
    assert overview["satisfaction_trend"] == [
        {"date": (now - timedelta(days=1)).strftime("%m-%d"), "value": 100.0}
    ]


def test_dify_endpoint_has_a_local_fallback_without_credentials(client: TestClient) -> None:
    headers = login(client)
    response = client.post(
        "/api/v1/dify/customer-service",
        headers=headers,
        json={"query": "请协助确认合同审批进度"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "local_fallback"
    assert payload["degraded"] is True
    assert payload["citations"]
    assert {step["step"] for step in payload["trace"]} >= {
        "Dify Gateway",
        "意图路由",
        "知识检索",
        "回答质检",
    }
    assert "合同" in payload["answer"]


def test_dify_endpoint_runs_local_agent_after_remote_failure(client: TestClient, monkeypatch) -> None:
    async def unavailable_gateway(query: str, user: str) -> DifyWorkflowResult:
        del query, user
        return DifyWorkflowResult(
            answer=None,
            mode="local_fallback",
            degraded=True,
            detail="远程 Dify 调用失败：ConnectError",
        )

    monkeypatch.setattr(api_shared.dify_gateway, "run_customer_service", unavailable_gateway)
    headers = login(client)
    response = client.post(
        "/api/v1/dify/customer-service",
        headers=headers,
        json={"query": "开票需要准备哪些材料？"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "local_fallback"
    assert payload["citations"]
    assert payload["trace"][0] == {
        "step": "Dify Gateway",
        "status": "fallback",
        "detail": "远程 Dify 调用失败：ConnectError",
    }
    assert "发票" in payload["answer"]


def test_dify_remote_response_allows_empty_local_evidence(client: TestClient, monkeypatch) -> None:
    async def successful_gateway(query: str, user: str) -> DifyWorkflowResult:
        del query, user
        return DifyWorkflowResult(
            answer="Dify 远程工作流回答",
            mode="remote",
            degraded=False,
            detail="Dify 工作流调用成功",
        )

    monkeypatch.setattr(api_shared.dify_gateway, "run_customer_service", successful_gateway)
    headers = login(client)
    response = client.post(
        "/api/v1/dify/customer-service",
        headers=headers,
        json={"query": "远程工作流问题"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "answer": "Dify 远程工作流回答",
        "mode": "remote",
        "degraded": False,
        "detail": "Dify 工作流调用成功",
        "citations": [],
        "trace": [],
    }
