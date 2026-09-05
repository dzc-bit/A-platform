"""Administration: users, settings, audit and moderation routes (mechanically split from app/api.py)."""

from .shared import (  # noqa: F401
    AISetting,
    APIRouter,
    AdminAuditLog,
    AdminAuditLogOut,
    AdminAuditLogPage,
    Conversation,
    ConversationAuditDetail,
    ConversationAuditSummary,
    Depends,
    HTTPException,
    Message,
    Query,
    SETTING_DEFAULTS,
    SETTING_DESCRIPTIONS,
    Session,
    SettingOut,
    SettingUpdate,
    User,
    UserCreate,
    UserOut,
    UserResetPassword,
    UserRoleUpdate,
    _audit_message_payload,
    _conversation_audit_payload,
    _conversation_audit_summary,
    datetime,
    func,
    get_db,
    hash_password,
    record_admin_action,
    require_roles,
    retrieval_cache,
    select,
    status,
    timezone,
    validate_setting,
)

router = APIRouter()

@router.get("/admin/users", response_model=list[UserOut], tags=["admin"])
def list_users(
    q: str | None = Query(default=None, max_length=100),
    role: str | None = Query(default=None),
    is_active: bool | None = Query(default=None),
    include_deleted: bool = Query(default=False),
    current_user: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> list[User]:
    del current_user
    stmt = select(User).order_by(User.created_at.desc())
    if not include_deleted:
        stmt = stmt.where(User.deleted_at.is_(None))
    if q:
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(User.display_name.ilike(pattern) | User.email.ilike(pattern))
    if role:
        stmt = stmt.where(User.role == role)
    if is_active is not None:
        stmt = stmt.where(User.is_active.is_(is_active))
    return list(db.scalars(stmt).all())


@router.post("/admin/users", response_model=UserOut, status_code=status.HTTP_201_CREATED, tags=["admin"])
def create_user(
    payload: UserCreate,
    current_user: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> User:
    email_normalized = payload.email.strip().lower()
    existing = db.scalar(select(User).where(User.email == email_normalized))
    if existing is not None:
        record_admin_action(
            db, current_user, "create_user",
            target_type="user", target_name=email_normalized,
            detail=f"角色={payload.role}", success=False, error_message="邮箱已存在",
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该邮箱已被注册")
    user = User(
        email=email_normalized,
        password_hash=hash_password(payload.password),
        display_name=payload.display_name.strip(),
        role=payload.role,
        is_active=payload.is_active,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    record_admin_action(
        db, current_user, "create_user",
        target_type="user", target_id=user.id, target_name=user.display_name,
        detail=f"邮箱={user.email}, 角色={user.role}",
    )
    return user


@router.post("/admin/users/{user_id}/reset-password", response_model=UserOut, tags=["admin"])
def reset_user_password(
    user_id: int,
    payload: UserResetPassword,
    current_user: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")
    user.password_hash = hash_password(payload.new_password)
    db.commit()
    db.refresh(user)
    record_admin_action(
        db, current_user, "reset_password",
        target_type="user", target_id=user.id, target_name=user.display_name,
        detail="管理员重置密码",
    )
    return user


@router.patch("/admin/users/{user_id}", response_model=UserOut, tags=["admin"])
def update_user(
    user_id: int,
    payload: UserRoleUpdate,
    current_user: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")
    # Self-protection: admin cannot deactivate or demote themselves.
    if user.id == current_user.id and (not payload.is_active or payload.role != "admin"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="不能停用或降级当前登录的管理员账户",
        )
    removes_active_admin = user.role == "admin" and user.is_active and (
        payload.role != "admin" or not payload.is_active
    )
    if removes_active_admin:
        active_admin_count = db.scalar(
            select(func.count(User.id)).where(
                User.role == "admin", User.is_active.is_(True), User.deleted_at.is_(None)
            )
        ) or 0
        if active_admin_count <= 1:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="系统必须保留至少一个已启用的管理员账户",
            )
    changes: list[str] = []
    if user.role != payload.role:
        changes.append(f"角色: {user.role} → {payload.role}")
    if user.is_active != payload.is_active:
        changes.append(f"状态: {'启用' if user.is_active else '停用'} → {'启用' if payload.is_active else '停用'}")
    user.role = payload.role
    user.is_active = payload.is_active
    db.commit()
    db.refresh(user)
    if changes:
        record_admin_action(
            db, current_user, "update_user",
            target_type="user", target_id=user.id, target_name=user.display_name,
            detail="; ".join(changes),
        )
    return user


@router.delete("/admin/users/{user_id}", response_model=UserOut, tags=["admin"])
def delete_user(
    user_id: int,
    current_user: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> User:
    """Soft-delete a user. Data (conversations, tickets, messages) is preserved."""
    user = db.get(User, user_id)
    if user is None or user.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")
    if user.id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="不能删除当前登录的管理员账户",
        )
    if user.role == "admin" and user.is_active:
        active_admin_count = db.scalar(
            select(func.count(User.id)).where(
                User.role == "admin", User.is_active.is_(True), User.deleted_at.is_(None)
            )
        ) or 0
        if active_admin_count <= 1:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="系统必须保留至少一个已启用的管理员账户",
            )
    user.deleted_at = datetime.now(timezone.utc)
    user.is_active = False
    db.commit()
    db.refresh(user)
    record_admin_action(
        db, current_user, "delete_user",
        target_type="user", target_id=user.id, target_name=user.display_name,
        detail=f"软删除用户，邮箱={user.email}",
    )
    return user


@router.get("/admin/settings", response_model=list[SettingOut], tags=["admin"])
def list_settings(
    current_user: User = Depends(require_roles("admin")), db: Session = Depends(get_db)
) -> list[AISetting]:
    del current_user
    return list(db.scalars(select(AISetting).order_by(AISetting.key)).all())


@router.put("/admin/settings/{key}", response_model=SettingOut, tags=["admin"])
def update_setting(
    key: str,
    payload: SettingUpdate,
    current_user: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> AISetting:
    try:
        value = validate_setting(key, payload.value)
    except ValueError as error:
        record_admin_action(
            db, current_user, "update_setting",
            target_type="setting", target_name=key,
            detail=f"值={payload.value[:100]}", success=False, error_message=str(error),
        )
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error
    setting = db.scalar(select(AISetting).where(AISetting.key == key))
    old_value = setting.value if setting else "(新建)"
    if setting is None:
        setting = AISetting(key=key, value=value, description=payload.description.strip())
        db.add(setting)
    else:
        setting.value = value
        setting.description = payload.description.strip()
    db.commit()
    db.refresh(setting)
    retrieval_cache.clear()
    record_admin_action(
        db, current_user, "update_setting",
        target_type="setting", target_name=key,
        detail=f"{old_value[:60]} → {value[:60]}",
    )
    return setting


@router.put("/admin/settings-reset", response_model=list[SettingOut], tags=["admin"])
def reset_settings(
    current_user: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> list[AISetting]:
    """Restore all AI settings to their factory defaults."""
    results: list[AISetting] = []
    for key, default_value in SETTING_DEFAULTS.items():
        setting = db.scalar(select(AISetting).where(AISetting.key == key))
        if setting is None:
            setting = AISetting(key=key, value=default_value, description=SETTING_DESCRIPTIONS.get(key, ""))
            db.add(setting)
        else:
            setting.value = default_value
            setting.description = SETTING_DESCRIPTIONS.get(key, "")
        results.append(setting)
    db.commit()
    for setting in results:
        db.refresh(setting)
    retrieval_cache.clear()
    record_admin_action(
        db, current_user, "reset_settings",
        target_type="setting", target_name="全部配置",
        detail=f"恢复 {len(SETTING_DEFAULTS)} 项配置为默认值",
    )
    return results


@router.get("/admin/audit-logs", response_model=AdminAuditLogPage, tags=["admin"])
def list_audit_logs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    action: str | None = Query(default=None),
    current_user: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> AdminAuditLogPage:
    del current_user
    stmt = select(AdminAuditLog).order_by(AdminAuditLog.created_at.desc())
    count_stmt = select(func.count(AdminAuditLog.id))
    if action:
        stmt = stmt.where(AdminAuditLog.action == action)
        count_stmt = count_stmt.where(AdminAuditLog.action == action)
    total = db.scalar(count_stmt) or 0
    items = list(db.scalars(stmt.offset((page - 1) * page_size).limit(page_size)).all())
    return AdminAuditLogPage(
        items=[AdminAuditLogOut.model_validate(item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/admin/conversations",
    response_model=list[ConversationAuditSummary],
    tags=["admin"],
)
def list_admin_conversations(
    current_user: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> list[ConversationAuditSummary]:
    """List every conversation as one compact row for the audit accordion."""
    del current_user
    conversations = db.scalars(select(Conversation).order_by(Conversation.updated_at.desc())).all()
    return [_conversation_audit_summary(db, conversation) for conversation in conversations]


@router.get("/admin/messages", tags=["admin"])
def recent_messages(
    current_user: User = Depends(require_roles("admin")), db: Session = Depends(get_db)
) -> list[dict[str, object]]:
    del current_user
    messages = db.scalars(select(Message).order_by(Message.created_at.desc()).limit(80)).all()
    return [_audit_message_payload(message) for message in messages]


@router.get(
    "/admin/conversations/{conversation_id}",
    response_model=ConversationAuditDetail,
    tags=["admin"],
)
def get_admin_conversation(
    conversation_id: int,
    current_user: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> ConversationAuditDetail:
    """Return any user's full transcript for administrator audit."""
    del current_user
    conversation = db.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")
    return ConversationAuditDetail.model_validate(
        _conversation_audit_payload(db, conversation, include_messages=True)
    )
