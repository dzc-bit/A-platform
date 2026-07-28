"""Admin operation audit trail service.

Records every privileged administrator action into the ``admin_audit_logs``
table for compliance review.  Each record captures who performed the action,
what was done, which object was affected, and whether it succeeded.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import AdminAuditLog, User


def record_admin_action(
    db: Session,
    admin: User,
    action: str,
    *,
    target_type: str = "",
    target_id: int | None = None,
    target_name: str = "",
    detail: str = "",
    success: bool = True,
    error_message: str = "",
) -> AdminAuditLog:
    """Persist one admin operation record.

    This intentionally commits in its own transaction so the audit trail
    survives even if the caller's outer transaction later rolls back (the
    failure itself is recorded with ``success=False``).
    """
    log = AdminAuditLog(
        admin_id=admin.id,
        admin_name=admin.display_name,
        action=action,
        target_type=target_type,
        target_id=target_id,
        target_name=target_name,
        detail=detail,
        success=success,
        error_message=error_message,
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return log
