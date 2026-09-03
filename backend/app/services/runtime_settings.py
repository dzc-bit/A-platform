from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import AISetting


DEFAULT_ASSISTANT_PROMPT = (
    "你是企业商务服务助手。只依据提供的企业知识与工具结果回答；"
    "不得编造订单、价格、合同或个人信息；无法确认时建议转人工。"
)

SETTING_DEFAULTS: dict[str, str] = {
    "assistant_prompt": DEFAULT_ASSISTANT_PROMPT,
    # Administrator-controlled defaults live alongside the model and prompt.
    "default_language": "zh-CN",
    "reply_strategy": "balanced",
    "llm_model": settings.llm_model,
    # The support drafting assistant has its own model/prompt contract.  It
    # may use retrieved snippets as references, but is not forced into the
    # enterprise assistant's fully grounded answer policy.
    "support_assistant_model": settings.llm_model,
    "support_assistant_prompt": (
        "你是客服人员的辅助 AI。请生成可供客服审核的回复草稿，结合通用业务常识和可选的企业知识片段；"
        "知识片段仅是参考，不要把未检索到的内容伪装成企业事实。不要自动发送消息。"
    ),
    "vision_model": settings.llm_vision_model or "",
    "retrieval_top_k": "3",
    "knowledge_chunk_size": "220",
    "knowledge_chunk_overlap": "40",
    "conversation_memory_messages": "6",
    "retrieval_cache_ttl_seconds": "300",
    "answer_cache_ttl_seconds": "300",
    "answer_groundedness_threshold": "0.15",
    "dify_status": "未配置，当前使用本地演示工作流",
}

SETTING_DESCRIPTIONS: dict[str, str] = {
    "default_language": "系统默认回答语言",
    "reply_strategy": "系统默认回复策略",
    "assistant_prompt": "企业用户对话的系统提示词",
    "llm_model": "文本对话使用的 OpenAI-compatible 模型名称",
    "support_assistant_model": "客服辅助 AI 使用的独立模型名称",
    "support_assistant_prompt": "客服辅助 AI 的系统提示词；输出仅供客服审核的草稿",
    "vision_model": "图片理解使用的视觉模型名称；留空时图片分析会安全回退",
    "retrieval_top_k": "每次对话最多引用的知识片段数（1-8）",
    "knowledge_chunk_size": "知识文档建立索引时的单个分块字符数（100-2000）",
    "knowledge_chunk_overlap": "相邻知识分块的重叠字符数（0-99）",
    "conversation_memory_messages": "每次模型调用带入的本会话历史消息数（0-12）",
    "retrieval_cache_ttl_seconds": "相同检索请求的缓存秒数（30-3600）",
    "answer_cache_ttl_seconds": "相同用户与上下文的最终回答缓存秒数（0-3600，0 为关闭）",
    "answer_groundedness_threshold": "回答与知识依据一致度质检阈值（0.01-1.00，低于阈值触发转人工建议）",
    "dify_status": "Dify 工作流连接状态说明",
}

_LANGUAGE_INSTRUCTIONS = {
    "zh-CN": "使用简体中文",
    "en-US": "使用英文",
}
_REPLY_STRATEGY_INSTRUCTIONS = {
    "concise": "简洁回答，优先给出结论和必要下一步",
    "balanced": "均衡回答，给出结论、依据和下一步",
    "detailed": "详细回答，清楚列出结论、依据、限制和下一步",
}


@dataclass(frozen=True)
class RuntimeSettings:
    assistant_prompt: str
    llm_model: str
    vision_model: str | None
    retrieval_top_k: int
    knowledge_chunk_size: int
    knowledge_chunk_overlap: int
    conversation_memory_messages: int
    retrieval_cache_ttl_seconds: int
    answer_cache_ttl_seconds: int
    answer_groundedness_threshold: float
    # Appended defaults keep positional construction by older integrations
    # compatible while exposing the administrator-controlled runtime knobs.
    default_language: str = "zh-CN"
    reply_strategy: str = "balanced"
    support_assistant_model: str = ""
    support_assistant_prompt: str = DEFAULT_ASSISTANT_PROMPT


def runtime_prompt_instruction(runtime_settings: RuntimeSettings) -> str:
    """Translate administrator defaults into a prompt-level runtime contract.

    The seeded defaults intentionally add no extra text so existing prompts stay
    stable.  A non-default administrator choice is appended by the Agent and is
    therefore effective for both normal and streaming calls.
    """
    parts: list[str] = []
    if runtime_settings.default_language != SETTING_DEFAULTS["default_language"]:
        parts.append(f"系统默认回答语言：{_LANGUAGE_INSTRUCTIONS[runtime_settings.default_language]}")
    if runtime_settings.reply_strategy != SETTING_DEFAULTS["reply_strategy"]:
        parts.append(f"系统默认回复策略：{_REPLY_STRATEGY_INSTRUCTIONS[runtime_settings.reply_strategy]}")
    return "；".join(parts)


def _validate_integer(key: str, value: str, minimum: int, maximum: int) -> str:
    try:
        parsed = int(value)
    except ValueError as error:
        raise ValueError(f"{key} 必须是 {minimum}-{maximum} 之间的整数") from error
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{key} 必须在 {minimum}-{maximum} 之间")
    return str(parsed)


def _validate_float(key: str, value: str, minimum: float, maximum: float) -> str:
    try:
        parsed = float(value)
    except ValueError as error:
        raise ValueError(f"{key} 必须是 {minimum}-{maximum} 之间的数值") from error
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{key} 必须在 {minimum}-{maximum} 之间")
    return f"{parsed:.2f}"


def validate_setting(key: str, value: str) -> str:
    if key not in SETTING_DEFAULTS:
        raise ValueError(f"不支持修改配置项：{key}")
    normalized = value.strip()
    if key == "assistant_prompt":
        if not 20 <= len(normalized) <= 10_000:
            raise ValueError("assistant_prompt 长度必须为 20-10000 个字符")
        return normalized
    if key == "llm_model":
        if not 1 <= len(normalized) <= 200:
            raise ValueError("llm_model 长度必须为 1-200 个字符")
        return normalized
    if key == "support_assistant_model":
        if not 1 <= len(normalized) <= 200:
            raise ValueError("support_assistant_model 长度必须为 1-200 个字符")
        return normalized
    if key == "support_assistant_prompt":
        if not 20 <= len(normalized) <= 10_000:
            raise ValueError("support_assistant_prompt 长度必须为 20-10000 个字符")
        return normalized
    if key == "vision_model":
        if not normalized:
            return ""
        if len(normalized) > 200:
            raise ValueError("vision_model 不能超过 200 个字符")
        return normalized
    if key == "default_language":
        if normalized not in {"zh-CN", "en-US"}:
            raise ValueError("default_language 只支持 zh-CN 或 en-US")
        return normalized
    if key == "reply_strategy":
        if normalized not in {"concise", "balanced", "detailed"}:
            raise ValueError("reply_strategy 只支持 concise/balanced/detailed")
        return normalized
    if key == "retrieval_top_k":
        return _validate_integer(key, normalized, 1, 8)
    if key == "knowledge_chunk_size":
        return _validate_integer(key, normalized, 100, 2000)
    if key == "knowledge_chunk_overlap":
        return _validate_integer(key, normalized, 0, 99)
    if key == "conversation_memory_messages":
        return _validate_integer(key, normalized, 0, 12)
    if key == "retrieval_cache_ttl_seconds":
        return _validate_integer(key, normalized, 30, 3600)
    if key == "answer_cache_ttl_seconds":
        return _validate_integer(key, normalized, 0, 3600)
    if key == "answer_groundedness_threshold":
        return _validate_float(key, normalized, 0.01, 1.0)
    if not normalized or len(normalized) > 255:
        raise ValueError("dify_status 不能为空且不能超过 255 个字符")
    return normalized


def ensure_runtime_settings(db: Session) -> int:
    existing = set(db.scalars(select(AISetting.key)).all())
    created = 0
    for key, default in SETTING_DEFAULTS.items():
        if key in existing:
            continue
        db.add(AISetting(key=key, value=default, description=SETTING_DESCRIPTIONS[key]))
        created += 1
    return created


def get_runtime_settings(db: Session) -> RuntimeSettings:
    values = SETTING_DEFAULTS.copy()
    for setting in db.scalars(select(AISetting).where(AISetting.key.in_(SETTING_DEFAULTS))).all():
        try:
            values[setting.key] = validate_setting(setting.key, setting.value)
        except ValueError:
            # A stale manually edited row must not break every user conversation.
            continue
    return RuntimeSettings(
        assistant_prompt=values["assistant_prompt"],
        default_language=values["default_language"],
        reply_strategy=values["reply_strategy"],
        llm_model=values["llm_model"],
        support_assistant_model=values["support_assistant_model"],
        support_assistant_prompt=values["support_assistant_prompt"],
        vision_model=values["vision_model"] or None,
        retrieval_top_k=int(values["retrieval_top_k"]),
        knowledge_chunk_size=int(values["knowledge_chunk_size"]),
        knowledge_chunk_overlap=int(values["knowledge_chunk_overlap"]),
        conversation_memory_messages=int(values["conversation_memory_messages"]),
        retrieval_cache_ttl_seconds=int(values["retrieval_cache_ttl_seconds"]),
        answer_cache_ttl_seconds=int(values["answer_cache_ttl_seconds"]),
        answer_groundedness_threshold=float(values["answer_groundedness_threshold"]),
    )
