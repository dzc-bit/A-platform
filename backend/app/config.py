from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


# Docker Compose passes variables directly; local development uses the same ignored
# root .env file without requiring a shell-specific export step.
load_dotenv(Path(__file__).resolve().parents[2] / ".env")


def _origins() -> list[str]:
    raw = os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _cache_key_prefix() -> str:
    return os.getenv("REDIS_KEY_PREFIX", "business-ai").strip() or "business-ai"


def _host_allowlist() -> tuple[str, ...]:
    """Return optional host suffixes permitted for remote media references.

    Dify media workflows may return signed URLs.  The allowlist is deliberately
    opt-in so an installation can keep its provider's host private; the gateway
    still rejects non-HTTP(S), localhost and private-address URLs regardless.
    """
    raw = os.getenv("DIFY_MEDIA_ALLOWED_HOSTS", "")
    return tuple(item.strip().lower() for item in raw.split(",") if item.strip())


def _dify_api_url() -> str | None:
    """Use the Docker host alias in containers and loopback for local Uvicorn.

    ``host.docker.internal`` is intentionally used by Compose, but on a host
    process it can resolve to an unreachable virtual adapter.  The local Dify
    stack is exposed on loopback in that mode.
    """
    value = (os.getenv("DIFY_API_URL") or "").strip()
    if not value:
        return None
    if "host.docker.internal" in value.lower() and not Path("/.dockerenv").exists():
        return value.replace("host.docker.internal", "127.0.0.1")
    return value


# Token secrets that ship with the repository as documented demo defaults.
# Anything in this set (or too short) must never reach a real deployment; the
# production gate in ``validate_production_secrets`` enforces that.
DEMO_TOKEN_SECRETS = frozenset(
    {
        "change-this-before-production",
        "replace-with-a-long-random-secret-before-production",
        "replace-with-a-long-random-secret",
    }
)
# The seeded demo accounts fall back to this value when DEMO_PASSWORD is unset.
DEMO_PASSWORD_DEFAULT = "replace-me-in-env"
_MIN_TOKEN_SECRET_LENGTH = 16


@dataclass(frozen=True)
class Settings:
    app_name: str = "东软智慧商务AI助手平台"
    api_prefix: str = "/api/v1"
    database_url: str = os.getenv(
        "DATABASE_URL",
        f"sqlite:///{Path(__file__).resolve().parents[1] / 'data' / 'business_ai.db'}",
    )
    cors_origins: tuple[str, ...] = tuple(_origins())
    token_secret: str = os.getenv("TOKEN_SECRET", "change-this-before-production")
    token_ttl_seconds: int = int(os.getenv("TOKEN_TTL_SECONDS", "28800"))
    # Fail-fast switch: when enabled, startup refuses to run with demo secrets.
    require_prod_secrets: bool = _env_flag("REQUIRE_PROD_SECRETS")
    demo_password: str = os.getenv("DEMO_PASSWORD", "")
    llm_api_key: str | None = os.getenv("LLM_API_KEY")
    llm_base_url: str = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
    llm_model: str = os.getenv("LLM_MODEL", "gpt-4o-mini")
    llm_vision_model: str | None = os.getenv("LLM_VISION_MODEL", "").strip() or None
    # Optional OpenAI-compatible embedding API (/embeddings). Unset keeps the
    # deterministic local hash embeddings; configuring it switches the stored
    # vector payloads and cache keys to the cloud embedding version.
    embedding_api_key: str | None = os.getenv("EMBEDDING_API_KEY")
    embedding_base_url: str = os.getenv("EMBEDDING_BASE_URL", "https://api.openai.com/v1")
    embedding_model: str = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
    redis_url: str | None = os.getenv("REDIS_URL")
    redis_key_prefix: str = _cache_key_prefix()
    dify_api_url: str | None = _dify_api_url()
    dify_api_key: str | None = os.getenv("DIFY_API_KEY")
    # Published Dify applications have independent API keys.  The specific
    # keys fall back to DIFY_API_KEY for installations that expose one app key
    # while still allowing a deployment to bind three separately published
    # workflows without putting keys in workflow YAML.
    dify_tts_api_key: str | None = os.getenv("DIFY_TTS_API_KEY")
    dify_image_api_key: str | None = os.getenv("DIFY_IMAGE_API_KEY")
    dify_media_allowed_hosts: tuple[str, ...] = _host_allowlist()
    # Dify router workflow: the entry-point workflow that classifies requests
    # and routes to knowledge base or LangGraph callback.
    dify_router_api_key: str | None = os.getenv("DIFY_ROUTER_API_KEY")
    dify_router_timeout_seconds: int = int(os.getenv("DIFY_ROUTER_TIMEOUT_SECONDS", "150"))
    # Shared secret for Dify HTTP callback node → FastAPI internal tool endpoint.
    dify_callback_secret: str | None = os.getenv("DIFY_CALLBACK_SECRET")


settings = Settings()


def validate_production_secrets(config: Settings = settings) -> list[str]:
    """Return the Chinese problem list that blocks a production deployment.

    An empty list means every secret passes the gate. The check covers
    TOKEN_SECRET (demo default or too short) and DEMO_PASSWORD (missing or
    still the seeded default), because both would let outsiders authenticate.
    """
    problems: list[str] = []
    if not config.token_secret or config.token_secret in DEMO_TOKEN_SECRETS:
        problems.append("TOKEN_SECRET 仍为演示默认值，必须更换为强随机密钥后才能对外提供服务。")
    elif len(config.token_secret) < _MIN_TOKEN_SECRET_LENGTH:
        problems.append(f"TOKEN_SECRET 长度不足 {_MIN_TOKEN_SECRET_LENGTH} 位，强度不满足生产要求。")
    if not config.demo_password or config.demo_password == DEMO_PASSWORD_DEFAULT:
        problems.append("DEMO_PASSWORD 未配置或仍为默认值，演示账号将使用公开口令，必须先更换。")
    return problems


def ensure_production_secrets(config: Settings = settings) -> None:
    """Raise RuntimeError listing every secret problem when the gate is on."""
    problems = validate_production_secrets(config)
    if problems:
        raise RuntimeError(
            "生产配置门禁未通过（REQUIRE_PROD_SECRETS 已开启），请先解决以下问题：\n- "
            + "\n- ".join(problems)
        )
