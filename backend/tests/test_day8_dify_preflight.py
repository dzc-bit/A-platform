from __future__ import annotations

import json

import pytest

from dify.day8_preflight import (
    DIFY_ROOT,
    _api_root,
    build_static_report,
    dsl_compatibility,
    dsl_import_gate,
    indexing_configuration,
    inspect_knowledge,
    inspect_official_docker,
    inspect_workflow_templates,
    load_acceptance_cases,
)

# These checks compare workflow templates against a vendored official Dify
# checkout under .runtime/dify, which is gitignored and only exists on
# machines that ran the local Dify stack.
requires_local_dify = pytest.mark.skipif(
    not (DIFY_ROOT / "docker" / "docker-compose.yaml").is_file()
    or not (DIFY_ROOT / "api" / "constants" / "dsl_version.py").is_file(),
    reason="local official Dify checkout (.runtime/dify) is not available",
)


@requires_local_dify
def test_day8_static_report_covers_official_stack_and_synthetic_corpus() -> None:
    report = build_static_report()

    assert report["passed"] is True
    assert report["acceptance_ready"] is False
    assert report["official_dify"]["runtime_checked"] is False
    assert report["knowledge"]["acceptance_cases"] == 16
    assert report["knowledge"]["acceptance_retrieval_disabled"] is True
    assert report["indexing"]["process_rule"]["rules"]["segmentation"] == {
        "separator": "\n\n",
        "max_tokens": 500,
        "chunk_overlap": 50,
    }
    assert report["indexing"]["retrieval_model"]["top_k"] == 3


def test_acceptance_questions_map_to_source_ids_without_uploading_answers() -> None:
    cases = load_acceptance_cases()

    assert len(cases) == 16
    assert {case.case_id for case in cases} == {f"AQ-{index:02d}" for index in range(1, 17)}
    assert all(case.source_ids for case in cases)
    assert any(case.forbidden_instruction for case in cases)
    _, _, checks = inspect_knowledge()
    assert all(check.passed for check in checks)


@requires_local_dify
def test_official_dify_compose_and_dsl_warning_are_reported() -> None:
    _, compose_checks = inspect_official_docker()
    dsl, dsl_checks = inspect_workflow_templates()

    assert all(check.passed for check in compose_checks)
    assert dsl["current_version"] == "0.6.0"
    assert {item["imported_version"] for item in dsl["templates"]} == {"0.3.0"}
    assert {item["compatibility"] for item in dsl["templates"]} == {"completed-with-warnings"}
    assert all(check.passed for check in dsl_checks)


def test_dsl_import_gate_requires_explicit_warning_acknowledgement() -> None:
    assert dsl_compatibility("0.3.0", "0.6.0") == "completed-with-warnings"
    assert dsl_compatibility("0.6.0", "0.6.0") == "completed"
    assert dsl_compatibility("1.0.0", "0.6.0") == "pending"
    assert dsl_import_gate("not-run")[0] is False
    assert dsl_import_gate("completed-with-warnings")[0] is False
    assert dsl_import_gate("completed-with-warnings", acknowledge_warning=True)[0] is True
    assert dsl_import_gate("completed")[0] is True


def test_dify_api_payload_is_explicit_and_does_not_require_a_secret() -> None:
    config = indexing_configuration()

    assert _api_root("http://localhost:8081") == "http://localhost:8081/v1"
    assert _api_root("http://localhost:8081/v1/") == "http://localhost:8081/v1"
    assert config["indexing_technique"] == "high_quality"
    assert config["embedding_model"] == "<configure-in-dify>"
    assert config["embedding_model_provider"] == "<configure-in-dify>"
    assert config["process_rule"]["mode"] == "custom"
    assert config["retrieval_model"]["search_method"] == "semantic_search"
    assert "DIFY_DATASET_API_KEY" not in json.dumps(config)
