from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


def _prepare_sqlite_directory() -> None:
    if not settings.database_url.startswith("sqlite:///"):
        return
    database_path = Path(settings.database_url.removeprefix("sqlite:///"))
    database_path.parent.mkdir(parents=True, exist_ok=True)


_prepare_sqlite_directory()
engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {},
    future=True,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_database() -> None:
    # Import models before metadata creation so all table declarations are registered.
    from . import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _ensure_demo_schema_compatibility()


def _ensure_demo_schema_compatibility() -> None:
    """Add the small set of nullable demo columns to an existing SQLite volume.

    The course stack intentionally does not use Alembic.  ``create_all`` does
    not alter an already-created SQLite table, so this idempotent bootstrap is
    needed when a user upgrades the demo without deleting its named volume.
    New databases already contain these columns and perform no work here.
    """
    if not settings.database_url.startswith("sqlite"):
        return
    required: dict[str, dict[str, str]] = {
        "conversations": {
            "handoff_status": "VARCHAR(24) DEFAULT 'ai'",
            "assigned_agent_id": "INTEGER",
            "takeover_by_id": "INTEGER",
            "takeover_notice": "TEXT",
            "takeover_at": "DATETIME",
            "feedback_rating": "INTEGER",
            "feedback_helpful": "BOOLEAN",
            "feedback_comment": "TEXT",
            "feedback_submitted_at": "DATETIME",
        },
        "support_tickets": {
            "requester_id": "INTEGER",
            "conversation_id": "INTEGER",
        },
        "messages": {
            "artifacts_json": "TEXT DEFAULT '[]'",
        },
        "users": {
            "deleted_at": "DATETIME",
        },
    }
    with engine.begin() as connection:
        inspector = inspect(connection)
        for table, columns in required.items():
            existing = {column["name"] for column in inspector.get_columns(table)}
            for column, definition in columns.items():
                if column not in existing:
                    connection.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {definition}'))
            # SQLite permits indexes to be created independently of the column
            # ALTER operation and IF NOT EXISTS keeps retries harmless.
            if table == "conversations":
                connection.execute(
                    text('CREATE INDEX IF NOT EXISTS "ix_conversations_handoff_status" ON "conversations" (handoff_status)')
                )
                connection.execute(
                    text('CREATE INDEX IF NOT EXISTS "ix_conversations_assigned_agent_id" ON "conversations" (assigned_agent_id)')
                )
                connection.execute(
                    text('CREATE INDEX IF NOT EXISTS "ix_conversations_takeover_by_id" ON "conversations" (takeover_by_id)')
                )
            else:
                connection.execute(
                    text('CREATE INDEX IF NOT EXISTS "ix_support_tickets_requester_id" ON "support_tickets" (requester_id)')
                )
                connection.execute(
                    text('CREATE INDEX IF NOT EXISTS "ix_support_tickets_conversation_id" ON "support_tickets" (conversation_id)')
                )
        # Admin audit log table for existing databases.
        connection.execute(
            text(
                'CREATE TABLE IF NOT EXISTS "admin_audit_logs" ('
                "id INTEGER PRIMARY KEY, "
                "admin_id INTEGER NOT NULL REFERENCES users(id), "
                "admin_name VARCHAR(80) DEFAULT '', "
                "action VARCHAR(64) NOT NULL, "
                "target_type VARCHAR(32) DEFAULT '', "
                "target_id INTEGER, "
                "target_name VARCHAR(160) DEFAULT '', "
                "detail TEXT DEFAULT '', "
                "success BOOLEAN DEFAULT 1, "
                "error_message TEXT DEFAULT '', "
                "created_at DATETIME DEFAULT (datetime('now'))"
                ")"
            )
        )
        connection.execute(
            text('CREATE INDEX IF NOT EXISTS "ix_admin_audit_logs_admin_id" ON "admin_audit_logs" (admin_id)')
        )
        connection.execute(
            text('CREATE INDEX IF NOT EXISTS "ix_admin_audit_logs_action" ON "admin_audit_logs" (action)')
        )
