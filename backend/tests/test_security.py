"""安全回归测试：令牌生命周期、防篡改、角色越权与资源隔离。

约定：
- 令牌格式与 ``app/security.py`` 保持一致（urlsafe base64 去padding 的
  ``payload.signature`` 两段式，payload 为 ``{"sub", "role", "exp"}`` 紧凑 JSON，
  签名为 HMAC-SHA256(payload_part, token_secret)）。此处有意在测试内自行拼接
  并签名，而不是只调用 ``create_access_token``，用于防止实现漂移后测试失效；
  需要“合法令牌”的场景仍直接复用 ``create_access_token``。
- 测试环境变量（token_secret、数据库等）由 ``conftest.py`` 在导入应用前钉死。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select

from .conftest import login
from app.config import settings
from app.database import SessionLocal
from app.models import User
from app.security import create_access_token, hash_password, verify_password

# 与 app/security.py / app/dependencies.py 中的中文错误信息逐字保持一致，
# 若生产代码改文案，这里应当一起评审是否属于破坏性变更。
AUTH_FAIL_DETAIL = "登录状态无效或已过期"
INACTIVE_ACCOUNT_DETAIL = "账户不可用"
NO_TOKEN_DETAIL = "请先登录后再访问该资源"
ROLE_DENIED_DETAIL = "当前角色没有此操作权限"
CONVERSATION_DENIED_DETAIL = "无权访问该会话"


def _b64url(value: bytes) -> str:
    """与 security.py 的 _b64encode 相同：urlsafe base64 且去掉 padding。"""
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _sign(payload_part: str, secret: str) -> str:
    """与 security.py create_access_token 相同的 HMAC-SHA256 签名（防漂移）。"""
    digest = hmac.new(
        secret.encode("utf-8"), payload_part.encode("ascii"), hashlib.sha256
    ).digest()
    return _b64url(digest)


def _craft_token(
    user_id: int,
    role: str,
    *,
    exp: int | None = None,
    secret: str | None = None,
) -> str:
    """按 security.py 的格式手工签发令牌，允许自定义 exp 与密钥。"""
    payload = {
        "sub": user_id,
        "role": role,
        "exp": exp if exp is not None else int(time.time()) + settings.token_ttl_seconds,
    }
    payload_part = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return f"{payload_part}.{_sign(payload_part, secret or settings.token_secret)}"


def _flip_one_char(segment: str) -> str:
    """把段内第一个字符替换成字母表中的另一个字符，保证 base64 仍然合法。"""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    for index, char in enumerate(segment):
        replacement = next(candidate for candidate in alphabet if candidate != char)
        return segment[:index] + replacement + segment[index + 1 :]
    raise AssertionError("segment must not be empty")


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _current_user_id(client: TestClient, headers: dict[str, str]) -> int:
    response = client.get("/api/v1/auth/me", headers=headers)
    assert response.status_code == 200, response.text
    return int(response.json()["id"])


def _set_user_flags(email: str, **flags: object) -> None:
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == email))
        assert user is not None
        for key, value in flags.items():
            setattr(user, key, value)
        db.commit()


# ---------------------------------------------------------------------------
# 1. 令牌过期
# ---------------------------------------------------------------------------


def test_expired_token_is_rejected_with_auth_failure_detail(client: TestClient) -> None:
    headers = login(client)
    user_id = _current_user_id(client, headers)

    # exp 明确位于过去：签名合法但已过期，必须被 decode_access_token 拒绝
    expired_token = _craft_token(user_id, "enterprise_user", exp=int(time.time()) - 60)
    response = client.get("/api/v1/auth/me", headers=_auth_headers(expired_token))

    assert response.status_code == 401, response.text
    assert response.json()["detail"] == AUTH_FAIL_DETAIL


# ---------------------------------------------------------------------------
# 2. 令牌篡改
# ---------------------------------------------------------------------------


def test_tampered_payload_is_rejected(client: TestClient) -> None:
    headers = login(client)
    user_id = _current_user_id(client, headers)
    token = create_access_token(user_id, "enterprise_user")
    payload_part, signature_part = token.split(".", maxsplit=1)

    # (a) 直接翻转 payload 段中的任意一个字节（base64 仍合法）→ 签名不再匹配
    tampered = f"{_flip_one_char(payload_part)}.{signature_part}"
    response = client.get("/api/v1/auth/me", headers=_auth_headers(tampered))
    assert response.status_code == 401, response.text
    assert response.json()["detail"] == AUTH_FAIL_DETAIL

    # (b) 重造合法 payload（试图把 role 提权为 admin）但沿用原签名 → 同样拒绝
    forged_payload = {"sub": user_id, "role": "admin", "exp": int(time.time()) + 60}
    forged_part = _b64url(json.dumps(forged_payload, separators=(",", ":")).encode("utf-8"))
    forged = f"{forged_part}.{signature_part}"
    response = client.get("/api/v1/auth/me", headers=_auth_headers(forged))
    assert response.status_code == 401, response.text
    assert response.json()["detail"] == AUTH_FAIL_DETAIL


def test_tampered_signature_is_rejected(client: TestClient) -> None:
    headers = login(client)
    user_id = _current_user_id(client, headers)
    token = create_access_token(user_id, "enterprise_user")
    payload_part, signature_part = token.split(".", maxsplit=1)

    tampered = f"{payload_part}.{_flip_one_char(signature_part)}"
    response = client.get("/api/v1/auth/me", headers=_auth_headers(tampered))

    assert response.status_code == 401, response.text
    assert response.json()["detail"] == AUTH_FAIL_DETAIL


# ---------------------------------------------------------------------------
# 3. 错误密钥签名
# ---------------------------------------------------------------------------


def test_token_signed_with_wrong_secret_is_rejected(client: TestClient) -> None:
    headers = login(client)
    user_id = _current_user_id(client, headers)

    forged = _craft_token(user_id, "enterprise_user", secret="attacker-controlled-secret")
    assert forged != create_access_token(user_id, "enterprise_user")

    response = client.get("/api/v1/auth/me", headers=_auth_headers(forged))

    assert response.status_code == 401, response.text
    assert response.json()["detail"] == AUTH_FAIL_DETAIL


# ---------------------------------------------------------------------------
# 4. 令牌有效但用户已失效（回库校验）
# ---------------------------------------------------------------------------


def test_valid_token_is_rejected_after_user_is_deactivated(client: TestClient) -> None:
    # 变体一：is_active 置 False
    headers = login(client)
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 200

    _set_user_flags("enterprise@neusoft.local", is_active=False)

    response = client.get("/api/v1/auth/me", headers=headers)
    assert response.status_code == 401, response.text
    assert response.json()["detail"] == INACTIVE_ACCOUNT_DETAIL


def test_valid_token_is_rejected_after_user_is_soft_deleted(client: TestClient) -> None:
    # 变体二：deleted_at 置为非空（软删除）
    headers = login(client)
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 200

    _set_user_flags("enterprise@neusoft.local", deleted_at=datetime.now(timezone.utc))

    response = client.get("/api/v1/auth/me", headers=headers)
    assert response.status_code == 401, response.text
    assert response.json()["detail"] == INACTIVE_ACCOUNT_DETAIL


# ---------------------------------------------------------------------------
# 5. 角色越权
# ---------------------------------------------------------------------------


def test_enterprise_user_cannot_access_admin_endpoint(client: TestClient) -> None:
    enterprise_headers = login(client)

    response = client.get("/api/v1/admin/users", headers=enterprise_headers)

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == ROLE_DENIED_DETAIL


def test_support_agent_cannot_access_admin_endpoint(client: TestClient) -> None:
    support_headers = login(client, "support@neusoft.local")

    response = client.get("/api/v1/admin/users", headers=support_headers)

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == ROLE_DENIED_DETAIL


def test_missing_token_is_unauthorized(client: TestClient) -> None:
    response = client.get("/api/v1/admin/users")

    assert response.status_code == 401, response.text
    assert response.json()["detail"] == NO_TOKEN_DETAIL
    assert response.headers.get("WWW-Authenticate") == "Bearer"


# ---------------------------------------------------------------------------
# 6. 跨用户资源隔离
# ---------------------------------------------------------------------------


def test_enterprise_users_cannot_read_each_others_conversations(client: TestClient) -> None:
    owner_headers = login(client)
    created = client.post(
        "/api/v1/assistant/chat",
        headers=owner_headers,
        json={"message": "开票申请需要准备什么材料？", "mode": "knowledge"},
    )
    assert created.status_code == 200, created.text
    conversation_id = created.json()["conversation_id"]

    registered = client.post(
        "/api/v1/auth/register",
        json={
            "email": "second-enterprise@example.test",
            "password": "StrongPass123!",
            "display_name": "另一位企业用户",
        },
    )
    assert registered.status_code == 201, registered.text
    intruder_headers = _auth_headers(registered.json()["access_token"])

    # _owned_conversation：会话存在但非本人 → 403（不是 404，避免掩盖资源存在性差异）
    intruder_response = client.get(
        f"/api/v1/assistant/conversations/{conversation_id}/messages",
        headers=intruder_headers,
    )
    assert intruder_response.status_code == 403, intruder_response.text
    assert intruder_response.json()["detail"] == CONVERSATION_DENIED_DETAIL

    # 拥有者本人访问不受影响
    owner_response = client.get(
        f"/api/v1/assistant/conversations/{conversation_id}/messages",
        headers=owner_headers,
    )
    assert owner_response.status_code == 200, owner_response.text
    assert isinstance(owner_response.json(), list)
    assert any(message["content"] == "开票申请需要准备什么材料？" for message in owner_response.json())


# ---------------------------------------------------------------------------
# 7. 密码哈希
# ---------------------------------------------------------------------------


def test_password_hashing_uses_unique_salt_and_verifies_correctly() -> None:
    password = "Str0ng!Passw0rd"

    first_stored = hash_password(password)
    second_stored = hash_password(password)

    # 同一密码两次哈希产生不同盐 → 存储串必然不同
    assert first_stored != second_stored
    salt_hex_first = first_stored.split("$", maxsplit=1)[0]
    salt_hex_second = second_stored.split("$", maxsplit=1)[0]
    assert salt_hex_first != salt_hex_second
    assert len(salt_hex_first) == 32  # 16 字节盐的 hex 长度

    # 校验：正确密码通过、错误密码拒绝
    assert verify_password(password, first_stored) is True
    assert verify_password(password, second_stored) is True
    assert verify_password(password + "x", first_stored) is False
    assert verify_password("", first_stored) is False
    assert verify_password(password, "not-a-valid-stored-hash") is False

    # 存储串不泄露明文
    assert password not in first_stored
    assert password not in second_stored
