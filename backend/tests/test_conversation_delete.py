from fastapi.testclient import TestClient

from .conftest import login


def test_enterprise_user_can_delete_only_owned_conversation(client: TestClient) -> None:
    owner = login(client)
    created = client.post(
        "/api/v1/assistant/chat",
        headers=owner,
        json={"message": "Create a conversation that can be removed."},
    )
    assert created.status_code == 200, created.text
    conversation_id = created.json()["conversation_id"]

    other = client.post(
        "/api/v1/auth/register",
        json={
            "email": "another-enterprise@example.test",
            "password": "StrongPass123!",
            "display_name": "Another user",
        },
    )
    assert other.status_code == 201, other.text
    other_headers = {"Authorization": f"Bearer {other.json()['access_token']}"}

    # A different enterprise user cannot delete the owner's conversation.
    forbidden = client.delete(
        f"/api/v1/assistant/conversations/{conversation_id}",
        headers=other_headers,
    )
    assert forbidden.status_code == 403, forbidden.text

    # Staff roles cannot use the enterprise conversation deletion operation.
    assert (
        client.delete(
            f"/api/v1/assistant/conversations/{conversation_id}",
            headers=login(client, "support@neusoft.local"),
        ).status_code
        == 403
    )
    assert (
        client.delete(
            f"/api/v1/assistant/conversations/{conversation_id}",
            headers=login(client, "admin@neusoft.local"),
        ).status_code
        == 403
    )

    deleted = client.delete(
        f"/api/v1/assistant/conversations/{conversation_id}",
        headers=owner,
    )
    assert deleted.status_code == 204, deleted.text
    assert not any(
        item["id"] == conversation_id
        for item in client.get("/api/v1/assistant/conversations", headers=owner).json()
    )
    assert (
        client.get(
            f"/api/v1/assistant/conversations/{conversation_id}/messages",
            headers=owner,
        ).status_code
        == 404
    )

    # Deletion is final and a stale repeat does not mutate another resource.
    repeated = client.delete(
        f"/api/v1/assistant/conversations/{conversation_id}",
        headers=owner,
    )
    assert repeated.status_code == 404, repeated.text
