from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[2]


def _size_bytes(value: str) -> int:
    match = re.fullmatch(r"(\d+)([kKmMgG]?)", value.strip())
    assert match is not None, value
    unit = match.group(2).lower()
    multiplier = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}[unit]
    return int(match.group(1)) * multiplier


def _release_step(name: str) -> dict[str, Any]:
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["deploy"]["steps"]
    return next(step for step in steps if step.get("name") == name)


def test_reverse_proxies_accept_the_documented_five_megabyte_upload_limit() -> None:
    nginx = (ROOT / "frontend/nginx.conf").read_text(encoding="utf-8")
    nginx_limit = re.search(r"\bclient_max_body_size\s+([^;]+);", nginx)
    assert nginx_limit is not None
    assert _size_bytes(nginx_limit.group(1)) > 5 * 1024**2

    ingress = yaml.safe_load((ROOT / "deploy/k8s/ingress.yaml").read_text(encoding="utf-8"))
    ingress_limit = ingress["metadata"]["annotations"].get(
        "nginx.ingress.kubernetes.io/proxy-body-size"
    )
    assert isinstance(ingress_limit, str)
    assert _size_bytes(ingress_limit) > 5 * 1024**2


def test_frontend_non_root_image_prepares_the_real_nginx_pid_directory() -> None:
    dockerfile = (ROOT / "frontend/Dockerfile").read_text(encoding="utf-8")

    assert "USER 101:101" in dockerfile
    assert "mkdir -p /run" in dockerfile
    assert re.search(r"chown\s+101:101\s+/run\b", dockerfile)
    assert "listen 8080;" in (ROOT / "frontend/nginx.conf").read_text(encoding="utf-8")


def test_kubernetes_declares_tls_and_private_registry_prerequisites() -> None:
    ingress = yaml.safe_load((ROOT / "deploy/k8s/ingress.yaml").read_text(encoding="utf-8"))
    ingress_host = ingress["spec"]["rules"][0]["host"]
    tls = ingress["spec"].get("tls")
    assert isinstance(tls, list) and len(tls) == 1
    assert tls[0]["hosts"] == [ingress_host]
    assert tls[0]["secretName"] == "business-ai-tls"

    for filename in ("backend.yaml", "frontend.yaml"):
        manifests = list(
            yaml.safe_load_all((ROOT / "deploy/k8s" / filename).read_text(encoding="utf-8"))
        )
        manifest = next(item for item in manifests if item["kind"] == "Deployment")
        assert manifest["spec"]["template"]["spec"]["imagePullSecrets"] == [
            {"name": "ghcr-pull-secret"}
        ]


def test_release_rejects_unsafe_token_secret_and_health_state() -> None:
    prerequisites = str(_release_step("Verify cluster prerequisites")["run"])
    assert ".data.TOKEN_SECRET" in prerequisites
    assert "DEPLOYMENT_URL" in prerequisites
    assert "ghcr-pull-secret" in prerequisites
    assert "business-ai-tls" in prerequisites

    health_step = _release_step("Check deployed HTTP endpoints")
    assert "if" not in health_step
    assert "using_demo_default" in str(health_step["run"])


def test_release_only_rolls_back_application_after_apply_has_started() -> None:
    apply_script = str(_release_step("Apply release and wait for rollouts")["run"])
    rollback_script = str(_release_step("Roll back application deployments on failure")["run"])

    assert "release-apply-started" in apply_script
    assert "release-apply-started" in rollback_script
    assert ".generation.before" in apply_script
    assert ".generation.before" in rollback_script
    assert 'rollout undo deployment/"$deployment"' in rollback_script
    assert "rollback_if_changed business-ai-backend" in rollback_script
    assert "rollback_if_changed business-ai-frontend" in rollback_script
    assert "rollback_if_changed business-ai-redis" not in rollback_script


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
