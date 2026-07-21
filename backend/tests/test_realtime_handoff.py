from __future__ import annotations

from fastapi.testclient import TestClient

from .conftest import login


def test_ticket_owner_scope_and_status_filter(client: TestClient) -> None:
    enterprise = login(client)
    created = client.post(
        "/api/v1/support/tickets",
        headers=enterprise,
        json={
            "customer_name": "Owner scope",
            "question": "I need an owner-scoped ticket for the live demo.",
            "priority": "normal",
        },
    )
    assert created.status_code == 201, created.text
    ticket_id = created.json()["id"]
    assert created.json()["suggested_reply"]

    mine = client.get("/api/v1/support/tickets/mine?status=pending", headers=enterprise)
    assert mine.status_code == 200, mine.text
    assert any(item["id"] == ticket_id for item in mine.json())

    support = login(client, "support@neusoft.local")
    assert client.get("/api/v1/support/tickets/mine", headers=support).status_code == 403
    resolved_without_reply = client.patch(
        f"/api/v1/support/tickets/{ticket_id}",
        headers=support,
        json={"status": "resolved"},
    )
    assert resolved_without_reply.status_code == 409

    resolved = client.patch(
        f"/api/v1/support/tickets/{ticket_id}",
        headers=support,
        json={"status": "resolved", "final_reply": "Closed after human review."},
    )
    assert resolved.status_code == 200, resolved.text
    assert client.get("/api/v1/support/tickets/mine?status=resolved", headers=enterprise).json()[0]["id"] == ticket_id


def test_ai_to_human_conversation_messages_are_scoped(client: TestClient) -> None:
    enterprise = login(client)
    created = client.post(
        "/api/v1/assistant/chat",
        headers=enterprise,
        json={"message": "Please start a conversation before handoff."},
    )
    assert created.status_code == 200, created.text
    conversation_id = created.json()["conversation_id"]

    handoff = client.post(
        f"/api/v1/assistant/conversations/{conversation_id}/handoff",
        headers=enterprise,
    )
    assert handoff.status_code == 200, handoff.text
    assert handoff.json()["status"] == "requested"
    assert handoff.json()["message"]["role"] == "system"

    support = login(client, "support@neusoft.local")
    queue = client.get("/api/v1/support/conversations", headers=support)
    assert queue.status_code == 200, queue.text
    assert any(item["id"] == conversation_id for item in queue.json())

    user_message = client.post(
        f"/api/v1/assistant/conversations/{conversation_id}/messages",
        headers=enterprise,
        json={"content": "Please connect me with a person."},
    )
    assert user_message.status_code == 200, user_message.text
    assert user_message.json()["role"] == "user"

    agent_message = client.post(
        f"/api/v1/support/conversations/{conversation_id}/messages",
        headers=support,
        json={"content": "您好，我已接入并开始处理。"},
    )
    assert agent_message.status_code == 200, agent_message.text
    assert agent_message.json()["role"] == "agent"

    messages = client.get(
        f"/api/v1/support/conversations/{conversation_id}/messages",
        headers=support,
    )
    assert [item["role"] for item in messages.json()] == ["user", "assistant", "system", "user", "agent"]

