from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from .conftest import login


def _create_conversation(client: TestClient, headers: dict[str, str]) -> int:
    response = client.post(
        "/api/v1/assistant/chat",
        headers=headers,
        json={"message": "请说明发票办理需要什么资料", "mode": "assistant"},
    )
    assert response.status_code == 200, response.text
    return response.json()["conversation_id"]


def test_enterprise_can_submit_one_owned_conversation_feedback(client: TestClient) -> None:
    enterprise_headers = login(client)
    conversation_id = _create_conversation(client, enterprise_headers)

    response = client.post(
        f"/api/v1/assistant/conversations/{conversation_id}/feedback",
        headers=enterprise_headers,
        json={"rating": 5, "helpful": True, "comment": "回答清楚，下一步建议有帮助。"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["rating"] == 5
    assert payload["helpful"] is True
    assert payload["comment"] == "回答清楚，下一步建议有帮助。"
    datetime.fromisoformat(payload["submitted_at"].replace("Z", "+00:00")).astimezone(timezone.utc)

    duplicate = client.post(
        f"/api/v1/assistant/conversations/{conversation_id}/feedback",
        headers=enterprise_headers,
        json={"rating": 3, "helpful": False},
    )
    assert duplicate.status_code == 409

    admin = client.post(
        f"/api/v1/assistant/conversations/{conversation_id}/feedback",
        headers=login(client, "admin@neusoft.local"),
        json={"rating": 4, "helpful": True},
    )
    assert admin.status_code == 403


def test_dashboard_uses_real_feedback_for_satisfaction(client: TestClient) -> None:
    enterprise_headers = login(client)
    conversation_id = _create_conversation(client, enterprise_headers)
    feedback = client.post(
        f"/api/v1/assistant/conversations/{conversation_id}/feedback",
        headers=enterprise_headers,
        json={"rating": 4, "helpful": True},
    )
    assert feedback.status_code == 200, feedback.text

    dashboard = client.get(
        "/api/v1/dashboard/overview",
        headers=login(client, "executive@neusoft.local"),
    )
    assert dashboard.status_code == 200, dashboard.text
    overview = dashboard.json()
    assert overview["feedback_count"] == 1
    assert overview["actual_ai_reply_satisfaction"] == 80.0
    assert overview["feedback_helpful_rate"] == 100.0
    assert overview["satisfaction_trend"][-1]["value"] == 80.0

    details = client.get(
        "/api/v1/dashboard/details",
        params={"scope": "satisfaction"},
        headers=login(client, "executive@neusoft.local"),
    )
    assert details.status_code == 200, details.text
    row = next(item for item in details.json()["rows"] if item.get("conversation_id") == conversation_id)
    assert row["rating"] == 4
    assert row["helpful"] is True
