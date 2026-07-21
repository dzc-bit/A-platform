from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import UserPreference


def get_user_preference(db: Session, user_id: int) -> UserPreference:
    preference = db.get(UserPreference, user_id)
    if preference is None:
        preference = UserPreference(user_id=user_id)
        db.add(preference)
        db.flush()
    return preference


def preference_instruction(preference: UserPreference) -> str:
    styles = {
        "concise": "简洁回答，优先给出结论和必要下一步",
        "balanced": "均衡回答，给出结论、依据和下一步",
        "detailed": "详细回答，清楚列出结论、依据、限制和下一步",
    }
    languages = {"zh-CN": "使用简体中文", "en-US": "使用英文"}
    return f"{languages[preference.preferred_language]}；{styles[preference.response_style]}。"
