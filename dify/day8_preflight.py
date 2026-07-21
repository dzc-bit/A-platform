"""Day 8 Dify knowledge-base preflight and opt-in API acceptance helper.

The default command is read-only.  It validates the checked-in Dify 1.15 Docker
bundle, the synthetic knowledge materials, and the retrieval configuration used
by the Day 8 lesson.  ``--apply`` is deliberately explicit because importing a
document or creating a dataset changes the user's Dify instance and requires a
dataset API token plus a configured embedding model.

The script talks only to the Dify service API.  It never reads the private
``.runtime/dify/docker/.env`` or the repository root ``.env`` and it never emits
API tokens or dataset IDs into a report.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

try:
    import httpx
except ImportError:  # pragma: no cover - only affects a static-only invocation
    httpx = None  # type: ignore[assignment]

try:
    import yaml
except ImportError:  # pragma: no cover - the backend test environment supplies PyYAML
    yaml = None  # type: ignore[assignment]


ROOT = Path(__file__).resolve().parents[1]
DIFY_ROOT = ROOT / ".runtime" / "dify"
DOCKER_ROOT = DIFY_ROOT / "docker"
COMPOSE_FILE = DOCKER_ROOT / "docker-compose.yaml"
ENV_EXAMPLE = DOCKER_ROOT / ".env.example"
KNOWLEDGE_ROOT = ROOT / "dify" / "knowledge"
SOURCE_FILE = KNOWLEDGE_ROOT / "official-business-support-sources.md"
ACCEPTANCE_FILE = KNOWLEDGE_ROOT / "acceptance-questions.md"

DIFY_VERSION = "1.15.0"
DEFAULT_DATASET_NAME = "business-support-day8"
DEFAULT_CHUNK_MAX_TOKENS = 500
DEFAULT_CHUNK_OVERLAP = 50
DEFAULT_TOP_K = 3
DEFAULT_SEARCH_METHOD = "semantic_search"

SOURCE_HEADING_RE = re.compile(r"^##\s+(S\d+)\.\s+(.+?)\s*$", re.MULTILINE)
QUESTION_HEADING_RE = re.compile(r"^###\s+(AQ-\d+)\s+(.+?)\s*$", re.MULTILINE)
SOURCE_REF_RE = re.compile(r"\bS\d+\b")
SECRET_RE = re.compile(r"(?i)(?:sk|app|dataset)-[A-Za-z0-9_-]{12,}")
BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+")
DATASET_PATH_RE = re.compile(r"(/datasets/)[^/\s]+")
DSL_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")
DSL_CURRENT_VERSION_RE = re.compile(r'CURRENT_APP_DSL_VERSION\s*=\s*["\']([^"\']+)["\']')


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class KnowledgeDocument:
    path: str
    role: str
    bytes: int
    sha256: str
    source_ids: tuple[str, ...]
    question_ids: tuple[str, ...]
    retrieval_enabled: bool


@dataclass(frozen=True)
class AcceptanceCase:
    case_id: str
    question: str
    source_ids: tuple[str, ...]
    forbidden_instruction: str | None


def _read_utf8(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_headings(text: str, pattern: re.Pattern[str]) -> tuple[str, ...]:
    return tuple(match.group(1) for match in pattern.finditer(text))


def _split_records(text: str, pattern: re.Pattern[str]) -> Iterable[tuple[str, str]]:
    matches = list(pattern.finditer(text))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        yield match.group(1), text[match.start() : end]


def load_acceptance_cases(path: Path = ACCEPTANCE_FILE) -> list[AcceptanceCase]:
    """Parse the regression questions without treating their answer text as corpus data."""

    text = _read_utf8(path)
    cases: list[AcceptanceCase] = []
    for case_id, block in _split_records(text, QUESTION_HEADING_RE):
        question = ""
        forbidden: str | None = None
        source_ids: tuple[str, ...] = ()
        # Use Unicode escapes so this helper remains safe to run on Windows
        # consoles whose active code page cannot encode the source language.
        question_label = "\u7528\u6237\u95ee\u9898"
        source_label = "\u5fc5\u987b\u5f15\u7528"
        forbidden_label = "\u7981\u6b62\u56de\u7b54"
        for line in block.splitlines():
            stripped = line.strip()
            if stripped.startswith("- ") and question_label in stripped:
                question = stripped.split("\uff1a", 1)[-1].strip()
            elif stripped.startswith("- ") and source_label in stripped:
                source_text = stripped.split("\uff1a", 1)[-1]
                source_ids = tuple(dict.fromkeys(SOURCE_REF_RE.findall(source_text)))
            elif stripped.startswith("- ") and forbidden_label in stripped:
                forbidden = stripped.split("\uff1a", 1)[-1].strip()
        if not question:
            raise ValueError(f"{path.name}: {case_id} has no user question")
        cases.append(
            AcceptanceCase(
                case_id=case_id,
                question=question,
                source_ids=source_ids,
                forbidden_instruction=forbidden,
            )
        )
    if not cases:
        raise ValueError(f"{path.name}: no acceptance questions found")
    return cases


def _dsl_version(path: Path) -> str:
    """Read the target Dify DSL version from the vendored official source."""

    source = DIFY_ROOT / "api" / "constants" / "dsl_version.py"
    if source.is_file():
        match = DSL_CURRENT_VERSION_RE.search(_read_utf8(source))
        if match:
            return match.group(1)
    raise ValueError(f"cannot determine current Dify DSL version from {source}")


def dsl_compatibility(imported: str, current: str) -> str:
    """Mirror Dify's ``check_version_compatibility`` result without importing its app."""

    imported_match = DSL_VERSION_RE.fullmatch(imported)
    current_match = DSL_VERSION_RE.fullmatch(current)
    if not imported_match or not current_match:
        return "failed"
    imported_tuple = tuple(int(item) for item in imported_match.groups())
    current_tuple = tuple(int(item) for item in current_match.groups())
    if imported_tuple > current_tuple or imported_tuple[0] < current_tuple[0]:
        return "pending"
    if imported_tuple[1] < current_tuple[1]:
        return "completed-with-warnings"
    return "completed"


def inspect_workflow_templates() -> tuple[dict[str, Any], list[Check]]:
    """Report DSL compatibility; do not rewrite templates without a target export."""

    checks: list[Check] = []
    current = _dsl_version(COMPOSE_FILE)
    templates: list[dict[str, Any]] = []
    for path in sorted((ROOT / "dify").glob("*-workflow.yml")):
        if yaml is None:
            checks.append(Check("dsl:yaml-parser", False, "PyYAML is required for DSL inspection"))
            break
        try:
            payload = yaml.safe_load(_read_utf8(path))
        except yaml.YAMLError as error:
            checks.append(Check(f"dsl:{path.name}:yaml", False, str(error)))
            continue
        imported = str(payload.get("version", "")) if isinstance(payload, dict) else ""
        compatibility = dsl_compatibility(imported, current)
        workflow = payload.get("workflow", {}) if isinstance(payload, dict) else {}
        graph = workflow.get("graph", {}) if isinstance(workflow, dict) else {}
        nodes = graph.get("nodes", []) if isinstance(graph, dict) else []
        edges = graph.get("edges", []) if isinstance(graph, dict) else []
        dependencies = payload.get("dependencies", []) if isinstance(payload, dict) else []
        missing_position_fields = sum(
            1 for node in nodes if isinstance(node, dict) and not {"sourcePosition", "targetPosition"} <= set(node)
        )
        templates.append(
            {
                "file": str(path.relative_to(ROOT)),
                "imported_version": imported,
                "current_version": current,
                "compatibility": compatibility,
                "node_count": len(nodes) if isinstance(nodes, list) else 0,
                "edge_count": len(edges) if isinstance(edges, list) else 0,
                "dependency_count": len(dependencies) if isinstance(dependencies, list) else 0,
                "modern_position_fields_missing": missing_position_fields,
                "conversion": "not-attempted",
            }
        )
        checks.append(
            Check(
                f"dsl:{path.name}:compatibility",
                compatibility in {"completed", "completed-with-warnings"},
                f"{imported} -> {current}: {compatibility}; import result must be checked in Dify",
            )
        )
        checks.append(
            Check(
                f"dsl:{path.name}:graph",
                bool(nodes) and bool(edges),
                f"{len(nodes)} nodes / {len(edges)} edges",
            )
        )
        checks.append(
            Check(
                f"dsl:{path.name}:dependencies",
                isinstance(dependencies, list),
                "dependency list is present; target workspace plugin versions still require manual verification",
            )
        )
    return {
        "current_version": current,
        "templates": templates,
        "import_status": "not-run",
        "warning_policy": (
            "0.3.x imports are completed-with-warnings on Dify 1.15; do not claim success "
            "until UI/API result is completed or the warning is acknowledged."
        ),
    }, checks


def dsl_import_gate(status: str, acknowledge_warning: bool = False) -> tuple[bool, str]:
    """Apply a conservative gate to a status copied from the Dify import response."""

    normalized = status.strip().lower()
    if normalized == "completed":
        return True, "Dify reported completed"
    if normalized == "completed-with-warnings":
        if acknowledge_warning:
            return True, "Dify reported completed-with-warnings and the warning was explicitly acknowledged"
        return False, "Dify reported completed-with-warnings; inspect/re-save the workflow and acknowledge the warning"
    if normalized == "not-run":
        return False, "workflow import has not been run against a Dify instance"
    return False, f"Dify workflow import status {status!r} is not an acceptance result"


def inspect_knowledge() -> tuple[list[KnowledgeDocument], list[AcceptanceCase], list[Check]]:
    """Validate the synthetic corpus and return a report-safe manifest."""

    checks: list[Check] = []
    documents: list[KnowledgeDocument] = []
    for path, role, retrieval_enabled in (
        (SOURCE_FILE, "source-corpus", True),
        (ACCEPTANCE_FILE, "regression-only", False),
    ):
        if not path.is_file():
            checks.append(Check(f"knowledge:{path.name}:exists", False, "file is missing"))
            continue
        text = _read_utf8(path)
        source_ids = _parse_headings(text, SOURCE_HEADING_RE)
        question_ids = _parse_headings(text, QUESTION_HEADING_RE)
        documents.append(
            KnowledgeDocument(
                path=str(path.relative_to(ROOT)),
                role=role,
                bytes=path.stat().st_size,
                sha256=_sha256(path),
                source_ids=source_ids,
                question_ids=question_ids,
                retrieval_enabled=retrieval_enabled,
            )
        )
        checks.append(
            Check(
                f"knowledge:{path.name}:exists-and-utf8",
                bool(text.strip()),
                f"{path.stat().st_size} bytes",
            )
        )
        checks.append(
            Check(
                f"knowledge:{path.name}:no-token-like-secret",
                SECRET_RE.search(text) is None,
                "no API-token pattern found",
            )
        )

    source_doc = next((item for item in documents if item.role == "source-corpus"), None)
    acceptance_doc = next((item for item in documents if item.role == "regression-only"), None)
    source_ids = set(source_doc.source_ids if source_doc else ())
    checks.append(Check("knowledge:source-ids", source_ids == {f"S{i}" for i in range(1, 9)},
                        "expected S1-S8"))
    cases: list[AcceptanceCase] = []
    if acceptance_doc and ACCEPTANCE_FILE.is_file():
        try:
            cases = load_acceptance_cases()
        except ValueError as error:
            checks.append(Check("knowledge:acceptance-question-map", False, str(error)))
        else:
            question_ids = {case.case_id for case in cases}
            expected = {f"AQ-{i:02d}" for i in range(1, 17)}
            checks.append(Check("knowledge:acceptance-question-ids", question_ids == expected,
                                "expected AQ-01..AQ-16"))
            bad_refs = sorted({ref for case in cases for ref in case.source_ids if ref not in source_ids})
            checks.append(Check("knowledge:acceptance-source-refs", not bad_refs,
                                "all references point to S1-S8" if not bad_refs else f"unknown refs: {bad_refs}"))
    checks.append(
        Check(
            "knowledge:regression-doc-disabled",
            bool(acceptance_doc and not acceptance_doc.retrieval_enabled),
            "acceptance questions are regression-only and must be disabled after import",
        )
    )
    return documents, cases, checks


def _env_names(path: Path) -> set[str]:
    names: set[str] = set()
    for line in _read_utf8(path).splitlines():
        match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if match:
            names.add(match.group(1))
    return names


def inspect_official_docker() -> tuple[dict[str, Any], list[Check]]:
    """Check the vendored official Compose bundle without starting containers."""

    checks: list[Check] = []
    details: dict[str, Any] = {
        "compose": str(COMPOSE_FILE.relative_to(ROOT)),
        "env_example": str(ENV_EXAMPLE.relative_to(ROOT)),
        "expected_version": DIFY_VERSION,
        "runtime_checked": False,
    }
    if yaml is None:
        return details, [Check("dify:yaml-parser", False, "PyYAML is required for compose inspection")]
    if not COMPOSE_FILE.is_file():
        return details, [Check("dify:compose-exists", False, "official compose file is missing")]
    if not ENV_EXAMPLE.is_file():
        checks.append(Check("dify:env-example-exists", False, "official env example is missing"))
    try:
        payload = yaml.safe_load(_read_utf8(COMPOSE_FILE))
    except yaml.YAMLError as error:
        return details, checks + [Check("dify:compose-yaml", False, str(error))]
    services = payload.get("services", {}) if isinstance(payload, dict) else {}
    required = {"api", "worker", "worker_beat", "web", "db_postgres", "redis", "weaviate", "nginx"}
    missing = sorted(required - set(services))
    checks.append(Check("dify:compose-services", not missing,
                        "required services present" if not missing else f"missing: {missing}"))
    expected_images = {
        "api": f"langgenius/dify-api:{DIFY_VERSION}",
        "worker": f"langgenius/dify-api:{DIFY_VERSION}",
        "worker_beat": f"langgenius/dify-api:{DIFY_VERSION}",
        "web": f"langgenius/dify-web:{DIFY_VERSION}",
    }
    image_mismatches = [
        f"{name}={services.get(name, {}).get('image')}"
        for name, expected in expected_images.items()
        if services.get(name, {}).get("image") != expected
    ]
    checks.append(Check("dify:version-pinned", not image_mismatches,
                        "api/web/worker images use 1.15.0" if not image_mismatches else "; ".join(image_mismatches)))
    api_health = str(services.get("api", {}).get("healthcheck", {}).get("test", ""))
    checks.append(Check("dify:api-healthcheck", "/health" in api_health, "api healthcheck targets /health"))
    redis_health = str(services.get("redis", {}).get("healthcheck", {}).get("test", ""))
    checks.append(Check("dify:redis-healthcheck", "redis-cli" in redis_health, "redis healthcheck uses redis-cli"))
    postgres_health = str(services.get("db_postgres", {}).get("healthcheck", {}).get("test", ""))
    checks.append(
        Check("dify:postgres-healthcheck", "pg_isready" in postgres_health, "postgres healthcheck uses pg_isready")
    )
    if ENV_EXAMPLE.is_file():
        names = _env_names(ENV_EXAMPLE)
        required_env = {
            "DIFY_PORT", "DB_TYPE", "VECTOR_STORE", "TOP_K_MAX_VALUE",
            "INDEXING_MAX_SEGMENTATION_TOKENS_LENGTH", "COMPOSE_PROFILES",
        }
        missing_env = sorted(required_env - names)
        checks.append(
            Check(
                "dify:env-contract",
                not missing_env,
                "startup, vector, and indexing variables present"
                if not missing_env
                else f"missing: {missing_env}",
            )
        )
    details["service_count"] = len(services)
    details["required_services"] = sorted(required)
    details["runtime_checked"] = False
    return details, checks


def indexing_configuration(
    *,
    indexing_technique: str = "high_quality",
    embedding_model: str | None = None,
    embedding_provider: str | None = None,
    chunk_max_tokens: int = DEFAULT_CHUNK_MAX_TOKENS,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    top_k: int = DEFAULT_TOP_K,
    search_method: str = DEFAULT_SEARCH_METHOD,
) -> dict[str, Any]:
    """Return the Dify 1.15 payload shared by dataset and document creation."""

    if indexing_technique not in {"high_quality", "economy"}:
        raise ValueError("indexing_technique must be high_quality or economy")
    if chunk_max_tokens <= 0 or chunk_max_tokens > 4000:
        raise ValueError("chunk_max_tokens must be between 1 and 4000")
    if chunk_overlap < 0 or chunk_overlap >= chunk_max_tokens:
        raise ValueError("chunk_overlap must be non-negative and smaller than chunk_max_tokens")
    if not 1 <= top_k <= 10:
        raise ValueError("top_k must be between 1 and 10")
    if indexing_technique == "high_quality" and (not embedding_model or not embedding_provider):
        # Static mode intentionally leaves model names blank.  The explicit
        # --apply path validates them before sending a mutating request.
        embedding_model = embedding_model or "<configure-in-dify>"
        embedding_provider = embedding_provider or "<configure-in-dify>"
    process_rule = {
        "mode": "custom",
        "rules": {
            "pre_processing_rules": [
                {"id": "remove_extra_spaces", "enabled": True},
                {"id": "remove_urls_emails", "enabled": False},
            ],
            "segmentation": {
                "separator": "\n\n",
                "max_tokens": chunk_max_tokens,
                "chunk_overlap": chunk_overlap,
            },
        },
    }
    retrieval_model = {
        "search_method": search_method,
        "reranking_enable": False,
        "reranking_model": {"reranking_provider_name": "", "reranking_model_name": ""},
        "top_k": top_k,
        "score_threshold_enabled": False,
    }
    return {
        "indexing_technique": indexing_technique,
        "embedding_model": embedding_model,
        "embedding_model_provider": embedding_provider,
        "retrieval_model": retrieval_model,
        "process_rule": process_rule,
        "doc_form": "text_model",
        "doc_language": "Chinese",
    }


def _redact(value: str) -> str:
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


def _redact_error(value: str) -> str:
    """Keep API diagnostics useful without echoing tokens or dataset identifiers."""

    value = BEARER_RE.sub(r"\1<redacted>", value)
    value = SECRET_RE.sub("<redacted-token>", value)
    return DATASET_PATH_RE.sub(r"\1<redacted>", value)


def _api_root(value: str) -> str:
    normalized = value.strip().rstrip("/")
    if normalized.endswith("/v1"):
        return normalized
    return f"{normalized}/v1"


class DifyDatasetClient:
    """Small synchronous client for the Dify 1.15 dataset service API."""

    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0) -> None:
        if httpx is None:
            raise RuntimeError("httpx is required for --apply")
        self.base_url = _api_root(base_url)
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout),
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        )

    def close(self) -> None:
        self._client.close()

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._client.request(method, path, **kwargs)
        if response.is_error:
            body = _redact_error(response.text[:400].replace("\n", " "))
            raise RuntimeError(f"Dify API {response.status_code} {_redact_error(path)}: {body}")
        if not response.content:
            return {}
        return response.json()

    @staticmethod
    def data(payload: Any) -> Any:
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

    def get_dataset(self, dataset_id: str) -> dict[str, Any]:
        payload = self.data(self.request("GET", f"/datasets/{dataset_id}"))
        if not isinstance(payload, dict):
            raise RuntimeError("Dify dataset response is not an object")
        return payload

    def create_dataset(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        result = self.data(self.request("POST", "/datasets", json=dict(payload)))
        if not isinstance(result, dict) or not result.get("id"):
            raise RuntimeError("Dify create dataset response did not contain an id")
        return result

    def list_documents(self, dataset_id: str) -> list[dict[str, Any]]:
        payload = self.data(self.request("GET", f"/datasets/{dataset_id}/documents", params={"limit": 100}))
        if isinstance(payload, dict):
            items = payload.get("data", [])
        else:
            items = payload
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    def create_document(self, dataset_id: str, name: str, text: str, config: Mapping[str, Any]) -> dict[str, Any]:
        payload = {
            "name": name,
            "text": text,
            "indexing_technique": config["indexing_technique"],
            "embedding_model": config.get("embedding_model"),
            "embedding_model_provider": config.get("embedding_model_provider"),
            "retrieval_model": config["retrieval_model"],
            "process_rule": config["process_rule"],
            "doc_form": config["doc_form"],
            "doc_language": config["doc_language"],
        }
        payload = {key: value for key, value in payload.items() if value is not None}
        result = self.data(self.request("POST", f"/datasets/{dataset_id}/document/create-by-text", json=payload))
        if not isinstance(result, dict):
            raise RuntimeError("Dify create document response is not an object")
        return result

    def indexing_status(self, dataset_id: str, batch: str) -> list[dict[str, Any]]:
        payload = self.data(self.request("GET", f"/datasets/{dataset_id}/documents/{batch}/indexing-status"))
        if isinstance(payload, dict):
            items = payload.get("data", [])
        else:
            items = payload
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    def update_document_status(self, dataset_id: str, action: str, document_ids: list[str]) -> None:
        if not document_ids:
            return
        self.request(
            "PATCH",
            f"/datasets/{dataset_id}/documents/status/{action}",
            json={"document_ids": document_ids},
        )

    def disable_documents(self, dataset_id: str, document_ids: list[str]) -> None:
        self.update_document_status(dataset_id, "disable", document_ids)

    def enable_documents(self, dataset_id: str, document_ids: list[str]) -> None:
        self.update_document_status(dataset_id, "enable", document_ids)

    def retrieve(self, dataset_id: str, query: str, retrieval_model: Mapping[str, Any]) -> dict[str, Any]:
        payload = self.data(
            self.request(
                "POST",
                f"/datasets/{dataset_id}/retrieve",
                json={"query": query, "retrieval_model": dict(retrieval_model)},
            )
        )
        if not isinstance(payload, dict):
            raise RuntimeError("Dify retrieval response is not an object")
        return payload


def _record_text(record: Mapping[str, Any]) -> str:
    parts: list[str] = []
    segment = record.get("segment")
    if isinstance(segment, Mapping):
        parts.append(str(segment.get("content", "")))
        document = segment.get("document")
        if isinstance(document, Mapping):
            parts.append(str(document.get("name", "")))
    parts.append(str(record.get("content", "")))
    return " ".join(parts)


def _wait_for_indexing(
    client: DifyDatasetClient,
    dataset_id: str,
    batch: str,
    timeout_seconds: float,
    poll_seconds: float,
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout_seconds
    last: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        last = client.indexing_status(dataset_id, batch)
        statuses = {str(item.get("indexing_status", "")) for item in last}
        if statuses and statuses <= {"completed"}:
            return last
        if "error" in statuses:
            return last
        time.sleep(poll_seconds)
    raise TimeoutError(f"indexing batch {_redact(batch)} did not complete within {timeout_seconds:g}s")


def run_remote(
    *,
    api_url: str,
    api_key: str,
    dataset_id: str | None,
    create_dataset: bool,
    dataset_name: str,
    config: Mapping[str, Any],
    upload_acceptance: bool,
    skip_upload: bool,
    question_limit: int | None,
    indexing_timeout: float,
    poll_seconds: float,
) -> dict[str, Any]:
    """Import the corpus and run Dify retrieval regression checks."""

    client = DifyDatasetClient(api_url, api_key)
    created = False
    try:
        if dataset_id:
            dataset = client.get_dataset(dataset_id)
        elif create_dataset:
            dataset = client.create_dataset(
                {
                    "name": dataset_name,
                    "description": "Synthetic Day 8 business-support corpus; acceptance file is regression-only.",
                    "indexing_technique": config["indexing_technique"],
                    "permission": "only_me",
                    "embedding_model": config.get("embedding_model"),
                    "embedding_model_provider": config.get("embedding_model_provider"),
                    "retrieval_model": config["retrieval_model"],
                }
            )
            dataset_id = str(dataset["id"])
            created = True
        else:
            raise ValueError("provide --dataset-id or --create-dataset for --apply")
        assert dataset_id is not None

        documents = client.list_documents(dataset_id)
        by_name = {str(item.get("name")): item for item in documents if item.get("name")}
        import_results: list[dict[str, Any]] = []
        upload_plan = [(SOURCE_FILE, True)]
        if upload_acceptance:
            upload_plan.append((ACCEPTANCE_FILE, False))
        if not skip_upload:
            for path, enabled in upload_plan:
                name = path.name
                existing = by_name.get(name)
                if existing:
                    item = {"name": name, "action": "existing", "document_id_present": bool(existing.get("id"))}
                    if existing.get("id"):
                        if not enabled and existing.get("enabled", True):
                            client.disable_documents(dataset_id, [str(existing["id"])])
                            item["disabled"] = True
                        elif enabled and existing.get("enabled") is False:
                            client.enable_documents(dataset_id, [str(existing["id"])])
                            item["enabled"] = True
                    import_results.append(item)
                    continue
                result = client.create_document(dataset_id, name, _read_utf8(path), config)
                batch = str(result.get("batch", ""))
                document = result.get("document") if isinstance(result.get("document"), Mapping) else {}
                status: list[dict[str, Any]] = []
                if batch:
                    status = _wait_for_indexing(client, dataset_id, batch, indexing_timeout, poll_seconds)
                document_id = str(document.get("id", ""))
                item = {
                    "name": name,
                    "action": "created",
                    "document_id_present": bool(document_id),
                    "indexing_status": sorted({str(row.get("indexing_status", "")) for row in status}),
                }
                if not enabled and document_id:
                    client.disable_documents(dataset_id, [document_id])
                    item["disabled"] = True
                import_results.append(item)

        _, cases, _ = inspect_knowledge()
        if question_limit is not None:
            cases = cases[:question_limit]
        retrieval_model = config["retrieval_model"]
        retrieval_results: list[dict[str, Any]] = []
        for case in cases:
            response = client.retrieve(dataset_id, case.question, retrieval_model)
            records = response.get("records", [])
            if not isinstance(records, list):
                records = []
            record_text = " ".join(_record_text(record) for record in records if isinstance(record, Mapping))
            matched = tuple(source for source in case.source_ids if source in record_text)
            retrieval_results.append(
                {
                    "case_id": case.case_id,
                    "expected_source_ids": list(case.source_ids),
                    "matched_source_ids": list(matched),
                    "record_count": len(records),
                    "passed": bool(matched),
                    "manual_safety_check": bool(case.forbidden_instruction),
                }
            )
        passed = sum(1 for result in retrieval_results if result["passed"])
        return {
            "status": "passed" if retrieval_results and passed == len(retrieval_results) else "failed",
            "dataset_id_present": True,
            "created_dataset": created,
            "documents": import_results,
            "retrieval": {"passed": passed, "total": len(retrieval_results), "cases": retrieval_results},
        }
    finally:
        client.close()


def build_static_report() -> dict[str, Any]:
    docker, docker_checks = inspect_official_docker()
    documents, cases, knowledge_checks = inspect_knowledge()
    dsl, dsl_checks = inspect_workflow_templates()
    config = indexing_configuration()
    checks = docker_checks + knowledge_checks + dsl_checks
    checks.append(Check("indexing:segmentation", config["process_rule"]["rules"]["segmentation"]["max_tokens"] <= 4000,
                        "custom separator, 500-token chunks, 50-token overlap"))
    checks.append(Check("indexing:retrieval", config["retrieval_model"]["top_k"] == 3,
                        "semantic search with top_k=3 and no reranker"))
    return {
        "kind": "day8_dify_preflight",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "static",
        "checks": [asdict(check) for check in checks],
        "passed": all(check.passed for check in checks),
        "acceptance_ready": False,
        "official_dify": docker,
        "dsl": dsl,
        "knowledge": {
            "documents": [asdict(document) for document in documents],
            "acceptance_cases": len(cases),
            "acceptance_retrieval_disabled": True,
        },
        "indexing": config,
        "runtime": {"status": "not_checked", "reason": "static preflight does not start Docker"},
    }


def _print_human(report: Mapping[str, Any]) -> None:
    status = "PASS" if report.get("passed") else "FAIL"
    print(f"{status} Day 8 Dify static preflight")
    for check in report.get("checks", []):
        marker = "PASS" if check.get("passed") else "FAIL"
        print(f"{marker} {check.get('name')}: {check.get('detail')}")
    runtime = report.get("runtime", {})
    print(f"RUNTIME {runtime.get('status')}: {runtime.get('reason', '')}")
    dsl_gate = report.get("dsl", {}).get("import_gate", {})
    if dsl_gate:
        marker = "PASS" if dsl_gate.get("passed") else "BLOCKED"
        print(f"{marker} DSL import gate: {dsl_gate.get('detail', '')}")
    ready = "ready" if report.get("acceptance_ready") else "not-ready"
    print(f"ACCEPTANCE {ready}: static checks do not replace target-instance evidence")
    if report.get("remote"):
        remote = report["remote"]
        retrieval = remote.get("retrieval", {})
        print(f"REMOTE {remote.get('status')}: retrieval {retrieval.get('passed')}/{retrieval.get('total')}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate and optionally exercise the Day 8 Dify knowledge API")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform explicit Dify dataset/document writes and retrieval tests",
    )
    parser.add_argument(
        "--api-url",
        default=os.getenv("DIFY_DATASET_API_URL", ""),
        help="Dify root URL; may end in /v1",
    )
    parser.add_argument("--dataset-id", default=os.getenv("DIFY_DATASET_ID", ""), help="existing dataset UUID")
    parser.add_argument("--create-dataset", action="store_true", help="create a dataset when --dataset-id is absent")
    parser.add_argument("--dataset-name", default=DEFAULT_DATASET_NAME)
    parser.add_argument(
        "--skip-upload",
        action="store_true",
        help="only run retrieval tests against an existing dataset",
    )
    parser.add_argument(
        "--no-acceptance-upload",
        action="store_true",
        help="do not import the regression-only document",
    )
    parser.add_argument("--indexing-technique", choices=("high_quality", "economy"), default="high_quality")
    parser.add_argument("--chunk-max-tokens", type=int, default=DEFAULT_CHUNK_MAX_TOKENS)
    parser.add_argument("--chunk-overlap", type=int, default=DEFAULT_CHUNK_OVERLAP)
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--question-limit", type=int)
    parser.add_argument("--indexing-timeout", type=float, default=900.0)
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument(
        "--dsl-import-status",
        choices=("not-run", "completed", "completed-with-warnings", "pending", "failed"),
        default="not-run",
        help="status copied from the target Dify import response; does not perform an import",
    )
    parser.add_argument(
        "--acknowledge-dsl-warning",
        action="store_true",
        help="accept a completed-with-warnings result after manually checking the target workflow",
    )
    parser.add_argument("--json-output", type=Path, help="write a redacted report to this path")
    args = parser.parse_args(argv)

    report = build_static_report()
    static_passed = bool(report["passed"])
    dsl_ok, dsl_detail = dsl_import_gate(args.dsl_import_status, args.acknowledge_dsl_warning)
    report["dsl"]["import_status"] = args.dsl_import_status
    report["dsl"]["import_gate"] = {"passed": dsl_ok, "detail": dsl_detail}
    # A static run intentionally remains a successful *preflight* while the
    # separate acceptance gate stays blocked until the target instance returns
    # an import status.  This keeps local checks useful without claiming import.
    report["acceptance_ready"] = bool(args.apply and static_passed and dsl_ok)
    if args.apply:
        if not args.api_url:
            print(
                "BLOCKED: set DIFY_DATASET_API_URL (for example http://localhost:8081) before --apply",
                file=sys.stderr,
            )
            return 2
        api_key = os.getenv("DIFY_DATASET_API_KEY", "")
        if not api_key:
            print("BLOCKED: set DIFY_DATASET_API_KEY in a private environment before --apply", file=sys.stderr)
            return 2
        embedding_model = os.getenv("DIFY_EMBEDDING_MODEL", "")
        embedding_provider = os.getenv("DIFY_EMBEDDING_PROVIDER", "")
        if args.indexing_technique == "high_quality" and (not embedding_model or not embedding_provider):
            print(
                "BLOCKED: configure a Dify text-embedding model/provider first, then set "
                "DIFY_EMBEDDING_MODEL and DIFY_EMBEDDING_PROVIDER privately",
                file=sys.stderr,
            )
            return 2
        config = indexing_configuration(
            indexing_technique=args.indexing_technique,
            embedding_model=embedding_model or None,
            embedding_provider=embedding_provider or None,
            chunk_max_tokens=args.chunk_max_tokens,
            chunk_overlap=args.chunk_overlap,
            top_k=args.top_k,
        )
        try:
            report["mode"] = "remote"
            report["remote"] = run_remote(
                api_url=args.api_url,
                api_key=api_key,
                dataset_id=args.dataset_id or None,
                create_dataset=args.create_dataset,
                dataset_name=args.dataset_name,
                config=config,
                upload_acceptance=not args.no_acceptance_upload,
                skip_upload=args.skip_upload,
                question_limit=args.question_limit,
                indexing_timeout=args.indexing_timeout,
                poll_seconds=args.poll_seconds,
            )
            report["acceptance_ready"] = bool(
                report["remote"]["status"] == "passed" and static_passed and dsl_ok
            )
        except (RuntimeError, TimeoutError, ValueError) as error:
            print(f"REMOTE FAILED: {error}", file=sys.stderr)
            return 1
    _print_human(report)
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    if args.apply:
        return 0 if report.get("acceptance_ready") else 1
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
