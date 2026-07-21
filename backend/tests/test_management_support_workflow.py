from __future__ import annotations

from fastapi.testclient import TestClient

from .conftest import login


def _handoff(client: TestClient) -> int:
    enterprise = login(client)
    created = client.post(
        "/api/v1/assistant/chat",
        headers=enterprise,
        json={"message": "Please connect this account question to a human agent."},
    )
    assert created.status_code == 200, created.text
    conversation_id = created.json()["conversation_id"]
    requested = client.post(
        f"/api/v1/assistant/conversations/{conversation_id}/handoff",
        headers=enterprise,
    )
    assert requested.status_code == 200, requested.text
    return conversation_id


def test_admin_can_upload_knowledge_through_management_path(client: TestClient) -> None:
    admin = login(client, "admin@neusoft.local")
    response = client.post(
        "/api/v1/admin/knowledge/upload",
        headers=admin,
        files={"file": ("manager.txt", "A management-only knowledge document with enough content.", "text/plain")},
    )
    assert response.status_code == 201, response.text
    assert response.json()["title"] == "manager.txt"
    assert client.post(
        "/api/v1/admin/knowledge/upload",
        headers=login(client, "executive@neusoft.local"),
        files={"file": ("denied.txt", "This must not be uploaded by an executive.", "text/plain")},
    ).status_code == 403


def test_support_assistant_is_private_and_can_skip_knowledge(client: TestClient) -> None:
    support = login(client, "support@neusoft.local")
    response = client.post(
        "/api/v1/support/assistant",
        headers=support,
        json={"query": "Draft a polite reply for an account access question.", "use_knowledge": False},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["model_mode"] == "support_general"
    assert payload["knowledge_used"] is False
    assert payload["citations"] == []
    assert client.post(
        "/api/v1/support/assistant",
        headers=login(client),
        json={"query": "This enterprise user must not use the support-only model."},
    ).status_code == 403


def test_executive_takeover_notifies_agent_and_blocks_agent_reply(client: TestClient) -> None:
    conversation_id = _handoff(client)
    executive = login(client, "executive@neusoft.local")
    agents = client.get("/api/v1/executive/support-agents", headers=executive)
    assert agents.status_code == 200, agents.text
    agent_id = next(item["id"] for item in agents.json() if item["email"] == "support@neusoft.local")

    takeover = client.post(
        f"/api/v1/executive/conversations/{conversation_id}/takeover",
        headers=executive,
        json={"assigned_agent_id": agent_id, "notice": "Management is handling this conversation."},
    )
    assert takeover.status_code == 200, takeover.text
    assert takeover.json()["takeover_by_id"] == client.get("/api/v1/auth/me", headers=executive).json()["id"]
    assert takeover.json()["control_mode"] == "executive_takeover"

    support = login(client, "support@neusoft.local")
    notifications = client.get("/api/v1/support/notifications?unread_only=true", headers=support)
    assert notifications.status_code == 200, notifications.text
    notice = next(item for item in notifications.json() if item["conversation_id"] == conversation_id)
    assert "Management" in notice["content"]
    assert notice["message"] == notice["content"]
    assert notice["agent_id"] == agent_id
    assert client.post(
        f"/api/v1/support/conversations/{conversation_id}/messages",
        headers=support,
        json={"content": "The support agent must not reply during executive takeover."},
    ).status_code == 403
    marked = client.post(f"/api/v1/support/notifications/{notice['id']}/read", headers=support)
    assert marked.status_code == 200
    assert marked.json()["is_read"] is True

    manager_message = client.post(
        f"/api/v1/executive/conversations/{conversation_id}/messages",
        headers=executive,
        json={"content": "Management has sent the confirmed response."},
    )
    assert manager_message.status_code == 200, manager_message.text
    assert manager_message.json()["sender_role"] == "executive"


def test_legacy_support_notification_alias_accepts_agent_id_and_message(client: TestClient) -> None:
    conversation_id = _handoff(client)
    executive = login(client, "executive@neusoft.local")
    agent_id = client.get("/api/v1/executive/support-agents", headers=executive).json()[0]["id"]
    response = client.post(
        f"/api/v1/support/conversations/{conversation_id}/notify",
        headers=executive,
        json={"agent_id": agent_id, "message": "Please receive this management notice."},
    )
    assert response.status_code == 200, response.text
    assert response.json()["message"] == "Please receive this management notice."
