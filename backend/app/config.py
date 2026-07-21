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
    llm_api_key: str | None = os.getenv("LLM_API_KEY")
    llm_base_url: str = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
    llm_model: str = os.getenv("LLM_MODEL", "gpt-4o-mini")
    llm_vision_model: str | None = os.getenv("LLM_VISION_MODEL", "").strip() or None
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


settings = Settings()
