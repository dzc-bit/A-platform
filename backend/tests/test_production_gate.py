"""Production secrets gate (REQUIRE_PROD_SECRETS) regression tests."""

from __future__ import annotations

import pytest

from app.config import (
    DEMO_PASSWORD_DEFAULT,
    DEMO_TOKEN_SECRETS,
    Settings,
    _env_flag,
    ensure_production_secrets,
    validate_production_secrets,
)
from app.routers import shared as shared_router

_STRONG_SECRET = "a-strong-random-production-secret"
_STRONG_DEMO_PASSWORD = "a-strong-demo-password-42"


def _gate_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "token_secret": _STRONG_SECRET,
        "demo_password": _STRONG_DEMO_PASSWORD,
    }
    base.update(overrides)
    return Settings(**base)


def test_strong_secrets_pass_the_gate() -> None:
    assert validate_production_secrets(_gate_settings()) == []


def test_every_documented_demo_token_secret_is_rejected() -> None:
    for demo_secret in DEMO_TOKEN_SECRETS:
        problems = validate_production_secrets(_gate_settings(token_secret=demo_secret))
        assert problems == ["TOKEN_SECRET 仍为演示默认值，必须更换为强随机密钥后才能对外提供服务。"]


def test_short_token_secret_is_rejected() -> None:
    problems = validate_production_secrets(_gate_settings(token_secret="short-secret"))
    assert len(problems) == 1
    assert "TOKEN_SECRET" in problems[0]
    assert "16" in problems[0]


def test_missing_demo_password_is_rejected() -> None:
    problems = validate_production_secrets(_gate_settings(demo_password=""))
    assert problems == ["DEMO_PASSWORD 未配置或仍为默认值，演示账号将使用公开口令，必须先更换。"]


def test_default_demo_password_is_rejected() -> None:
    problems = validate_production_secrets(_gate_settings(demo_password=DEMO_PASSWORD_DEFAULT))
    assert len(problems) == 1
    assert "DEMO_PASSWORD" in problems[0]


def test_gate_reports_all_problems_at_once() -> None:
    problems = validate_production_secrets(_gate_settings(token_secret="", demo_password=""))
    assert len(problems) == 2
    assert any("TOKEN_SECRET" in item for item in problems)
    assert any("DEMO_PASSWORD" in item for item in problems)


def test_ensure_production_secrets_raises_with_chinese_problem_list() -> None:
    with pytest.raises(RuntimeError) as excinfo:
        ensure_production_secrets(_gate_settings(token_secret="short", demo_password=""))
    message = str(excinfo.value)
    assert "生产配置门禁未通过" in message
    assert "TOKEN_SECRET" in message
    assert "DEMO_PASSWORD" in message


def test_ensure_production_secrets_passes_silently_with_strong_config() -> None:
    ensure_production_secrets(_gate_settings())


def test_demo_token_secrets_constant_lives_in_config_and_is_reexported() -> None:
    # shared.py must no longer define its own copy; both namespaces share one
    # frozenset so the /system security report and the gate can never diverge.
    assert shared_router.DEMO_TOKEN_SECRETS is DEMO_TOKEN_SECRETS


@pytest.mark.parametrize("raw,expected", [("1", True), ("true", True), ("YES", True), ("on", True), ("", False), ("0", False), ("false", False)])
def test_env_flag_parsing(raw: str, expected: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REQUIRE_PROD_SECRETS", raw)
    assert _env_flag("REQUIRE_PROD_SECRETS") is expected
