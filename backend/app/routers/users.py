"""User preferences routes (mechanically split from app/api.py)."""

from .shared import (  # noqa: F401
    APIRouter,
    Depends,
    Session,
    User,
    UserPreferenceOut,
    UserPreferenceUpdate,
    get_current_user,
    get_db,
    get_user_preference,
)

router = APIRouter()

@router.get("/users/me/preferences", response_model=UserPreferenceOut, tags=["users"])
def get_current_preferences(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    preference = get_user_preference(db, current_user.id)
    db.commit()
    db.refresh(preference)
    return preference


@router.put("/users/me/preferences", response_model=UserPreferenceOut, tags=["users"])
def update_current_preferences(
    payload: UserPreferenceUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    preference = get_user_preference(db, current_user.id)
    preference.response_style = payload.response_style
    preference.preferred_language = payload.preferred_language
    preference.auto_play_voice = payload.auto_play_voice
    db.commit()
    db.refresh(preference)
    return preference
