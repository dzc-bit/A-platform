from __future__ import annotations

from fastapi.testclient import TestClient

from .conftest import login


def _create_conversation(client: TestClient, headers: dict[str, str]) -> int:
    response = client.post(
        "/api/v1/assistant/chat",
        headers=headers,
        json={"message": "请说明开票申请需要哪些材料。", "mode": "assistant"},
    )
    assert response.status_code == 200, response.text
    return int(response.json()["conversation_id"])


def test_admin_audit_is_grouped_and_can_open_any_user_conversation(client: TestClient) -> None:
    enterprise_headers = login(client)
    conversation_id = _create_conversation(client, enterprise_headers)

    admin_headers = login(client, "admin@neusoft.local")
    listing = client.get("/api/v1/admin/conversations", headers=admin_headers)
    assert listing.status_code == 200, listing.text
    row = next(item for item in listing.json() if item["id"] == conversation_id)
    assert row["customer_email"] == "enterprise@neusoft.local"
    assert row["message_count"] >= 2
    assert row["recent_message"]["sender_label"] == "AI"
    assert row["recent_message"]["conversation_id"] == conversation_id
    legacy = client.get("/api/v1/admin/messages", headers=admin_headers)
    assert legacy.status_code == 200, legacy.text
    assert any(message["conversation_id"] == conversation_id for message in legacy.json())

    detail = client.get(f"/api/v1/admin/conversations/{conversation_id}", headers=admin_headers)
    assert detail.status_code == 200, detail.text
    payload = detail.json()
    assert payload["id"] == conversation_id
    assert len(payload["messages"]) == payload["message_count"]
    assert {message["sender_label"] for message in payload["messages"]} >= {"企业用户", "AI"}
    assert all("trace" in message and "citations" in message for message in payload["messages"])

    for email in ("enterprise@neusoft.local", "support@neusoft.local", "executive@neusoft.local"):
        denied = client.get(
            "/api/v1/admin/conversations",
            headers=login(client, email),
        )
        assert denied.status_code == 403


def test_dashboard_details_are_read_only_and_role_scoped(client: TestClient) -> None:
    enterprise_headers = login(client)
    conversation_id = _create_conversation(client, enterprise_headers)
    executive_headers = login(client, "executive@neusoft.local")

    expected_scopes = {
        "tickets": {"question", "status", "category", "quality_score"},
        "status": {"question", "status", "category"},
        "category": {"question", "status", "category"},
        "consultations": {"conversation_id", "content", "role", "customer_name"},
        "satisfaction": {"quality_score", "question", "status"},
        "insights": {"content", "title"},
    }
    for scope, fields in expected_scopes.items():
        response = client.get(
            "/api/v1/dashboard/details",
            params={"scope": scope},
            headers=executive_headers,
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["scope"] == scope
        assert payload["title"]
        assert payload["summary"]
        assert payload["rows"]
        assert fields.issubset(payload["rows"][0])

    consultation_rows = client.get(
        "/api/v1/dashboard/details",
        params={"scope": "consultations"},
        headers=executive_headers,
    ).json()["rows"]
    assert any(row["conversation_id"] == conversation_id for row in consultation_rows)

    pending = client.get(
        "/api/v1/dashboard/details",
        params={"scope": "tickets", "status": "pending"},
        headers=executive_headers,
    )
    assert pending.status_code == 200, pending.text
    assert all(row["status"] != "resolved" for row in pending.json()["rows"])
    category = pending.json()["rows"][0]["category"]
    category_rows = client.get(
        "/api/v1/dashboard/details",
        params={"scope": "tickets", "category": category},
        headers=executive_headers,
    )
    assert category_rows.status_code == 200, category_rows.text
    assert category_rows.json()["rows"]
    assert all(row["category"] == category for row in category_rows.json()["rows"])
    assert client.get(
        "/api/v1/dashboard/details",
        params={"scope": "tickets", "status": "all"},
        headers=executive_headers,
    ).status_code == 200

    transcript = client.get(
        f"/api/v1/dashboard/conversations/{conversation_id}",
        headers=executive_headers,
    )
    assert transcript.status_code == 200, transcript.text
    assert transcript.json()["messages"]
    assert client.get(
        "/api/v1/dashboard/details",
        params={"scope": "not-supported"},
        headers=executive_headers,
    ).status_code == 422

    assert client.get("/api/v1/dashboard/details", headers=login(client)).status_code == 403
    assert client.get("/api/v1/dashboard/conversations/1", headers=login(client, "support@neusoft.local")).status_code == 403
