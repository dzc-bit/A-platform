"""Authentication routes (mechanically split from app/api.py)."""

from .shared import (  # noqa: F401
    APIRouter,
    AuthResponse,
    Depends,
    HTTPException,
    LoginRequest,
    RegisterRequest,
    Session,
    User,
    UserOut,
    _auth_response,
    get_current_user,
    get_db,
    hash_password,
    select,
    status,
    verify_password,
)

router = APIRouter()

@router.post("/auth/register", response_model=AuthResponse, status_code=status.HTTP_201_CREATED, tags=["auth"])
def register(payload: RegisterRequest, db: Session = Depends(get_db)) -> AuthResponse:
    email = payload.email.strip().lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该邮箱已注册")
    user = User(
        email=email,
        password_hash=hash_password(payload.password),
        display_name=payload.display_name.strip(),
        role="enterprise_user",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return _auth_response(user)


@router.post("/auth/login", response_model=AuthResponse, tags=["auth"])
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> AuthResponse:
    user = db.scalar(select(User).where(User.email == payload.email.strip().lower()))
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="邮箱或密码错误")
    if user.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="该账户已注销")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="该账户已停用")
    return _auth_response(user)


@router.get("/auth/me", response_model=UserOut, tags=["auth"])
def current_profile(current_user: User = Depends(get_current_user)) -> User:
    return current_user
