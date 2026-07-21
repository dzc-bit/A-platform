from __future__ import annotations

import asyncio

from app.services.llm import Completion
from app.services.vision import VisionService


class CapturingVisionClient:
    def __init__(self, completion: Completion) -> None:
        self.completion = completion
        self.image_data_url = ""
        self.model = ""

    async def complete_vision(
        self,
        _system_prompt: str,
        _user_prompt: str,
        image_data_url: str,
        *,
        model: str | None = None,
    ) -> Completion:
        self.image_data_url = image_data_url
        self.model = model or ""
        return self.completion


def test_vision_service_encodes_transient_image_and_returns_provider_answer() -> None:
    client = CapturingVisionClient(Completion(text="图片显示红色状态提示。", used_fallback=False))
    result = asyncio.run(
        VisionService(llm_client=client).analyze(
            b"\x89PNG\r\n\x1a\nfixture",
            "image/png",
            "识别状态",
            model="qwen3-vl-plus",
        )
    )

    assert result.used_fallback is False
    assert result.answer.startswith("图片显示")
    assert client.image_data_url.startswith("data:image/png;base64,")
    assert client.model == "qwen3-vl-plus"


def test_vision_service_returns_cautious_fallback_when_provider_is_unavailable() -> None:
    client = CapturingVisionClient(Completion(text=None, used_fallback=True, reason="图片模型调用失败：ConnectError"))
    result = asyncio.run(
        VisionService(llm_client=client).analyze(
            b"\x89PNG\r\n\x1a\nfixture",
            "image/png",
            "",
            model="qwen3-vl-plus",
        )
    )

    assert result.used_fallback is True
    assert "无法可靠识别" in result.answer
    assert result.detail == "图片模型调用失败：ConnectError"
