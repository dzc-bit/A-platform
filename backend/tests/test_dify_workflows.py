from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml


DIFY_DIR = Path(__file__).resolve().parents[2] / "dify"


def _workflow(name: str) -> dict[str, Any]:
    payload = yaml.safe_load((DIFY_DIR / name).read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _nodes(payload: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = payload["workflow"]["graph"]["nodes"]
    assert isinstance(nodes, list)
    return nodes


def _node_types(payload: dict[str, Any]) -> set[str]:
    return {str(node["data"]["type"]) for node in _nodes(payload)}


def _node_by_id(payload: dict[str, Any], node_id: str) -> dict[str, Any]:
    return next(node for node in _nodes(payload) if node["id"] == node_id)


def _environment_names(payload: dict[str, Any]) -> set[str]:
    variables = payload["workflow"].get("environment_variables", [])
    return {str(variable["name"]) for variable in variables}


def test_business_support_workflow_covers_classification_conditions_code_and_http() -> None:
    payload = _workflow("business-support-workflow.yml")

    assert {
        "start",
        "question-classifier",
        "knowledge-retrieval",
        "code",
        "if-else",
        "http-request",
        "llm",
        "end",
    } <= _node_types(payload)
    assert "PLATFORM_API_BASE_URL" in _environment_names(payload)

    edges = payload["workflow"]["graph"]["edges"]
    for edge in edges:
        assert isinstance(edge["sourceHandle"], str), edge["id"]
        assert isinstance(edge["targetHandle"], str), edge["id"]
    connected = {(edge["source"], edge["target"]) for edge in edges}
    assert ("start", "classifier") in connected
    assert ("evidence_gate", "evidence_branch") in connected
    assert ("health_check", "retrieval") in connected
    assert ("health_check", "safe_handoff") in connected
    assert ("answerer", "answer_end") in connected
    assert ("safe_handoff", "handoff_end") in connected

    health_check = _node_by_id(payload, "health_check")
    assert health_check["data"]["error_strategy"] == "fail-branch"
    failure_edge = next(edge for edge in edges if edge["id"] == "health-handoff")
    assert failure_edge["sourceHandle"] == "fail-branch"


def test_tts_and_image_workflows_only_return_external_http_results() -> None:
    cases = (
        ("text-to-speech-workflow.yml", "TTS_API_URL", "tts_request", "audio"),
        ("text-to-image-workflow.yml", "IMAGE_API_URL", "image_request", "image"),
    )

    for filename, url_variable, request_id, output_name in cases:
        payload = _workflow(filename)
        assert {"start", "code", "http-request", "end"} <= _node_types(payload)
        assert url_variable in _environment_names(payload)

        request_node = _node_by_id(payload, request_id)
        assert f"env.{url_variable}" in str(request_node["data"]["url"])
        body = request_node["data"]["body"]
        assert body["type"] == "json"
        assert isinstance(body["data"], list)
        assert len(body["data"]) == 1
        body_text = body["data"][0]["value"]
        for token, replacement in {
            "{{#prepare_text.text#}}": "text",
            "{{#prepare_text.voice#}}": "voice",
            "{{#prepare_prompt.prompt#}}": "prompt",
            "{{#prepare_prompt.size#}}": "size",
        }.items():
            body_text = body_text.replace(token, replacement)
        parsed_body = json.loads(body_text)
        assert isinstance(parsed_body, dict)
        if output_name == "audio":
            assert parsed_body["model"] == "qwen3-tts-flash"
            assert set(parsed_body["input"]) >= {"text", "voice", "language_type"}
            assert _node_by_id(payload, "extract_audio")["data"]["type"] == "code"
        else:
            assert parsed_body["model"] == "qwen-image-2.0-pro"
            message = parsed_body["input"]["messages"][0]
            assert message["role"] == "user"
            assert message["content"][0]["text"] == "prompt"
            assert _node_by_id(payload, "extract_image")["data"]["type"] == "code"

        end_nodes = [node for node in _nodes(payload) if node["data"]["type"] == "end"]
        assert len(end_nodes) == 1
        outputs = end_nodes[0]["data"]["outputs"]
        output = next(item for item in outputs if item["variable"] == output_name)
        assert output["value_selector"][0] == ("extract_audio" if output_name == "audio" else "extract_image")


def test_workflow_templates_do_not_embed_service_credentials_or_localhost_urls() -> None:
    for path in DIFY_DIR.glob("*-workflow.yml"):
        text = path.read_text(encoding="utf-8")
        assert "localhost" not in text
        assert "127.0.0.1" not in text
        assert re.search(r"(?:sk|app)-[A-Za-z0-9]{12,}", text) is None

        payload = yaml.safe_load(text)
        for variable in payload["workflow"].get("environment_variables", []):
            if variable.get("value_type") == "secret":
                assert variable.get("value", "") == ""
