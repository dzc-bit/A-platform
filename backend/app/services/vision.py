from __future__ import annotations

import base64
from dataclasses import dataclass

from .llm import OpenAICompatibleClient


VISION_SYSTEM_PROMPT = (
    "你是企业商务服务的图片理解助手。只描述图片中清晰可见的文字、表格、物品或界面信息；"
    "不要识别或推断个人身份、账号、订单、付款或合同的真实数据。"
    "如果图片包含敏感信息，提醒用户遮蔽后再由人工核验。"
)
DEFAULT_IMAGE_QUESTION = "请说明图片中与当前企业服务问题相关的可见信息，并给出谨慎的下一步建议。"


@dataclass(frozen=True)
class VisionAnalysisResult:
    answer: str
    used_fallback: bool
    detail: str


class VisionService:
    """Keep image bytes transient and return a non-invented fallback on provider failure."""

    def __init__(self, llm_client: OpenAICompatibleClient | None = None) -> None:
        self._llm_client = llm_client or OpenAICompatibleClient()

    async def analyze(
        self,
        image_bytes: bytes,
        media_type: str,
        prompt: str,
        *,
        model: str | None,
    ) -> VisionAnalysisResult:
        image_data_url = f"data:{media_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
        completion = await self._llm_client.complete_vision(
            VISION_SYSTEM_PROMPT,
            prompt.strip() or DEFAULT_IMAGE_QUESTION,
            image_data_url,
            model=model,
        )
        if completion.text:
            return VisionAnalysisResult(
                answer=completion.text,
                used_fallback=False,
                detail="已通过视觉模型分析用户主动上传的图片；图片未被保存。",
            )
        return VisionAnalysisResult(
            answer=(
                "当前无法可靠识别这张图片。请改用文字描述，或请管理员配置可用的图片理解模型后重试。"
                "不要上传包含个人信息、订单明细、支付凭据或未授权合同内容的图片。"
            ),
            used_fallback=True,
            detail=completion.reason or "图片理解服务不可用，已保留文本流程。",
        )
