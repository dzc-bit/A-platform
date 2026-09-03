from __future__ import annotations

import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def _size_bytes(value: str) -> int:
    match = re.fullmatch(r"(\d+)([kKmMgG]?)", value.strip())
    assert match is not None, value
    unit = match.group(2).lower()
    multiplier = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}[unit]
    return int(match.group(1)) * multiplier


def test_reverse_proxies_accept_the_documented_five_megabyte_upload_limit() -> None:
    nginx = (ROOT / "frontend/nginx.conf").read_text(encoding="utf-8")
    nginx_limit = re.search(r"\bclient_max_body_size\s+([^;]+);", nginx)
    assert nginx_limit is not None
    assert _size_bytes(nginx_limit.group(1)) > 5 * 1024**2


def test_frontend_non_root_image_prepares_the_real_nginx_pid_directory() -> None:
    dockerfile = (ROOT / "frontend/Dockerfile").read_text(encoding="utf-8")

    assert "USER 101:101" in dockerfile
    assert "mkdir -p /run" in dockerfile
    assert re.search(r"chown\s+101:101\s+/run\b", dockerfile)
    assert "listen 8080;" in (ROOT / "frontend/nginx.conf").read_text(encoding="utf-8")


def test_stack_runner_keeps_official_dify_compose_separate_and_exposes_real_smoke_checks() -> None:
    script = (ROOT / "scripts/stack.ps1").read_text(encoding="utf-8")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))

    assert 'ValidateSet("up", "down", "ps", "health", "smoke", "config")' in script
    assert '"neusoft-business-ai"' in script
    assert '"neusoft-dify"' in script
    assert "DIFY_COMPOSE_DIR" in script
    assert "DIFY_COMPOSE_PROJECT_NAME" in script
    assert "DifyComposeDir" in script and "DifyProjectName" in script
    assert '"docker-compose.yaml"' in script
    assert "Invoke-DifyCompose" in script and "Invoke-AppCompose" in script
    assert "http://host.docker.internal:$port" in script
    assert '"up", "-d", "--wait", "--wait-timeout"' in script
    assert '"up", "-d", "--build", "--wait", "--wait-timeout"' in script
    assert '@("exec", "-T", "nginx", "nginx", "-s", "reload")' in script
    assert "http://127.0.0.1:$port/" in script
    assert "http://127.0.0.1:$port/console/api/setup" in script
    assert "frontend proxy -> FastAPI -> local RAG/agent" in script
    assert "DIFY_API_KEY" not in script
    assert "stack-up:" in makefile
    assert "stack-health:" in makefile
    assert "stack-smoke:" in makefile
    assert "stack-config:" in makefile

    backend = compose["services"]["backend"]
    assert "host.docker.internal:host-gateway" in backend["extra_hosts"]
    frontend = compose["services"]["frontend"]
    assert frontend["healthcheck"]["test"] == [
        "CMD-SHELL",
        "wget -q -O /dev/null http://127.0.0.1:8080/ || exit 1",
    ]
