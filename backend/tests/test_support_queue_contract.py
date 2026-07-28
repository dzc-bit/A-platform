from __future__ import annotations

from fastapi.testclient import TestClient

from .conftest import login


def test_support_history_excludes_ai_only_conversations(client: TestClient) -> None:
    enterprise = login(client)
    ai_only = client.post(
        "/api/v1/assistant/chat",
        headers=enterprise,
        json={"message": "Keep this conversation with the AI assistant."},
    )
    assert ai_only.status_code == 200, ai_only.text

    handed_off = client.post(
        "/api/v1/assistant/chat",
        headers=enterprise,
        json={"message": "Create a conversation that will be handed to support."},
    )
    assert handed_off.status_code == 200, handed_off.text
    handed_off_id = handed_off.json()["conversation_id"]
    assert client.post(
        f"/api/v1/assistant/conversations/{handed_off_id}/handoff",
        headers=enterprise,
    ).status_code == 200

    support = login(client, "support@neusoft.local")
    history = client.get("/api/v1/support/conversations?status=all", headers=support)
    assert history.status_code == 200, history.text
    conversation_ids = {item["id"] for item in history.json()}
    assert handed_off_id in conversation_ids
    assert ai_only.json()["conversation_id"] not in conversation_ids


def test_support_queue_exposes_customer_metadata_and_human_actions(client: TestClient) -> None:
    enterprise = login(client)
    created = client.post(
        "/api/v1/assistant/chat",
        headers=enterprise,
        json={"message": "I need a human agent for this account question."},
    )
    assert created.status_code == 200, created.text
    conversation_id = created.json()["conversation_id"]
    handoff = client.post(
        f"/api/v1/assistant/conversations/{conversation_id}/handoff",
        headers=enterprise,
    )
    assert handoff.status_code == 200, handoff.text

    linked_ticket = client.post(
        "/api/v1/support/tickets",
        headers=enterprise,
        json={
            "customer_name": "Queue customer",
            "question": "Please review this linked human support request.",
            "priority": "urgent",
            "conversation_id": conversation_id,
        },
    )
    assert linked_ticket.status_code == 201, linked_ticket.text

    support = login(client, "support@neusoft.local")
    queue = client.get("/api/v1/support/conversations", headers=support)
    assert queue.status_code == 200, queue.text
    row = next(item for item in queue.json() if item["id"] == conversation_id)
    assert row["customer_name"]
    assert row["customer_email"]
    assert row["unread_count"] == 0
    assert row["priority"] == "urgent"
    assert row["related_ticket_id"] == linked_ticket.json()["id"]
    assert row["recent_message"]["sender_label"] == "系统"

    # A customer turn after handoff becomes one unread human message.
    user_message = client.post(
        f"/api/v1/assistant/conversations/{conversation_id}/messages",
        headers=enterprise,
        json={"content": "Can someone take this over now?"},
    )
    assert user_message.status_code == 200
    assert user_message.json()["sender_label"] == "企业用户"

    queue = client.get("/api/v1/support/conversations", headers=support)
    row = next(item for item in queue.json() if item["id"] == conversation_id)
    assert row["unread_count"] == 1

    support_profile = client.get("/api/v1/auth/me", headers=support).json()
    assigned = client.patch(
        f"/api/v1/support/conversations/{conversation_id}",
        headers=support,
        json={"assigned_agent_id": support_profile["id"]},
    )
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["assigned_agent_id"] == support_profile["id"]
    assert assigned.json()["status"] == "active"

    agent_message = client.post(
        f"/api/v1/support/conversations/{conversation_id}/messages",
        headers=support,
        json={"content": "A support agent is reviewing this now."},
    )
    assert agent_message.status_code == 200
    assert agent_message.json()["role"] == "agent"
    assert agent_message.json()["sender_role"] == "support_agent"
    assert agent_message.json()["sender_label"] == "客服"

    marked_read = client.post(
        f"/api/v1/support/conversations/{conversation_id}/read",
        headers=support,
    )
    assert marked_read.status_code == 200
    assert marked_read.json()["unread_count"] == 0

    ended = client.post(
        f"/api/v1/support/conversations/{conversation_id}/end",
        headers=support,
    )
    assert ended.status_code == 200, ended.text
    assert ended.json()["status"] == "closed"
    assert not any(item["id"] == conversation_id for item in client.get("/api/v1/support/conversations", headers=support).json())
    history = client.get("/api/v1/support/conversations?status=all", headers=support)
    assert any(item["id"] == conversation_id and item["status"] == "closed" for item in history.json())


def test_support_can_claim_with_omitted_assignment_and_use_status_route(client: TestClient) -> None:
    enterprise = login(client)
    created = client.post(
        "/api/v1/assistant/chat",
        headers=enterprise,
        json={"message": "Please create another conversation for assignment testing."},
    )
    assert created.status_code == 200, created.text
    conversation_id = created.json()["conversation_id"]
    assert client.post(
        f"/api/v1/assistant/conversations/{conversation_id}/handoff",
        headers=enterprise,
    ).status_code == 200

    support = login(client, "support@neusoft.local")
    support_id = client.get("/api/v1/auth/me", headers=support).json()["id"]
    claimed = client.patch(
        f"/api/v1/support/conversations/{conversation_id}",
        headers=support,
        json={},
    )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["assigned_agent_id"] == support_id
    assert claimed.json()["status"] == "active"

    released = client.patch(
        f"/api/v1/support/conversations/{conversation_id}/status",
        headers=support,
        json={"status": "requested"},
    )
    assert released.status_code == 200, released.text
    assert released.json()["assigned_agent_id"] is None
    assert released.json()["status"] == "requested"
