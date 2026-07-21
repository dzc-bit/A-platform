from __future__ import annotations

import re

from fastapi.testclient import TestClient
from sqlalchemy import delete

from .conftest import login
from app.database import SessionLocal
from app.models import SupportTicket


def _replace_tickets() -> None:
    """Install a small, explicit status matrix for contract assertions."""
    with SessionLocal() as db:
        db.execute(delete(SupportTicket))
        db.add_all(
            [
                SupportTicket(
                    customer_name="Open high",
                    question="An unresolved high priority request.",
                    category="test",
                    priority="high",
                    status="open",
                    suggested_reply="Please review this request.",
                    quality_score=0.9,
                ),
                SupportTicket(
                    customer_name="In progress normal",
                    question="An unresolved normal priority request.",
                    category="test",
                    priority="normal",
                    status="in_progress",
                    suggested_reply="Please continue the review.",
                    quality_score=0.9,
                ),
                SupportTicket(
                    customer_name="Open normal",
                    question="Another unresolved normal priority request.",
                    category="test",
                    priority="normal",
                    status="open",
                    suggested_reply="Please review this request.",
                    quality_score=0.9,
                ),
                SupportTicket(
                    customer_name="Resolved high",
                    question="A resolved high priority request.",
                    category="test",
                    priority="high",
                    status="resolved",
                    suggested_reply="This request is closed.",
                    final_reply="Closed by support.",
                    quality_score=0.9,
                ),
                SupportTicket(
                    customer_name="Resolved urgent",
                    question="A resolved urgent request.",
                    category="test",
                    priority="urgent",
                    status="resolved",
                    suggested_reply="This request is closed.",
                    final_reply="Closed by support.",
                    quality_score=0.9,
                ),
            ]
        )
        db.commit()


def test_dashboard_report_excludes_resolved_high_priority_tickets(client: TestClient) -> None:
    """The high-priority figure is a subset of the unresolved queue."""
    _replace_tickets()

    response = client.get(
        "/api/v1/dashboard/report",
        headers=login(client, "admin@neusoft.local"),
    )

    assert response.status_code == 200, response.text
    summary = response.json()["summary"]
    # The report currently contains the unresolved total followed by its
    # high/urgent subset. Resolved high/urgent rows must not inflate either
    # interpretation of the pending queue.
    assert [int(value) for value in re.findall(r"\d+", summary)] == [3, 1]


def test_support_ticket_list_preserves_resolved_status(client: TestClient) -> None:
    """The full list preserves status and the optional filter owns queue semantics."""
    _replace_tickets()

    headers = login(client, "support@neusoft.local")
    response = client.get(
        "/api/v1/support/tickets",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    tickets = response.json()
    assert {ticket["status"] for ticket in tickets} == {"open", "in_progress", "resolved"}
    assert sum(ticket["status"] != "resolved" for ticket in tickets) == 3
    assert sum(ticket["status"] == "resolved" for ticket in tickets) == 2

    pending = client.get("/api/v1/support/tickets?status=pending", headers=headers)
    assert pending.status_code == 200, pending.text
    assert {ticket["status"] for ticket in pending.json()} == {"open", "in_progress"}

    resolved = client.get("/api/v1/support/tickets?status=resolved", headers=headers)
    assert resolved.status_code == 200, resolved.text
    assert resolved.json() and all(ticket["status"] == "resolved" for ticket in resolved.json())


def test_dashboard_exposes_explicit_ticket_status_totals(client: TestClient) -> None:
    _replace_tickets()

    response = client.get(
        "/api/v1/dashboard/overview",
        headers=login(client, "executive@neusoft.local"),
    )

    assert response.status_code == 200, response.text
    statuses = response.json()["ticket_statuses"]
    assert statuses == {
        "total": 5,
        "pending": 2,
        "open": 2,
        "in_progress": 1,
        "resolved": 2,
        "urgent": 1,
    }
    assert response.json()["ticket_counts"] == statuses
    assert response.json()["urgent_tickets"] == 1
    assert isinstance(response.json()["ai_reply_satisfaction"], float)
