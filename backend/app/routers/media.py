"""Voice capabilities and Dify workflow/media endpoints routes (mechanically split from app/api.py)."""

from . import shared
from .shared import *  # noqa: F401,F403

router = APIRouter()

@router.get("/voice/capabilities", tags=["voice"])
def voice_capabilities(current_user: User = Depends(get_current_user)) -> dict[str, object]:
    del current_user
    return {
        "input": "浏览器 Web Speech API（客户端能力检测后启用）",
        "output": "浏览器 speechSynthesis（客户端本地朗读）",
        "fallback": "浏览器不支持时保留完整文本输入和回复流程",
    }


# ---------------------------------------------------------------------------
# Internal LangGraph callback (called by Dify router workflow HTTP node)
# ---------------------------------------------------------------------------


@router.post(
    "/tools/langgraph/run",
    response_model=LangGraphCallbackResponse,
    tags=["internal"],
    include_in_schema=False,
)
async def langgraph_callback(
    payload: LangGraphCallbackRequest,
    db: Session = Depends(get_db),
    x_dify_callback_secret: str | None = Header(default=None),
) -> LangGraphCallbackResponse:
    """Internal endpoint for Dify router workflow HTTP callback node.

    Security: verified via shared secret header, not user JWT.
    Recursion guard: route_depth > 1 is rejected.
    """
    started = time.monotonic()

    # 1. Verify shared secret.
    expected_secret = shared.settings.dify_callback_secret
    if not expected_secret:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="回调服务未配置")
    if not x_dify_callback_secret or not secrets.compare_digest(x_dify_callback_secret, expected_secret):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="认证失败")

    # 2. Recursion guard.
    if payload.route_depth > 1:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="route_depth 超限，拒绝递归调用")

    if payload.conversation_id is not None:
        conversation = db.get(Conversation, payload.conversation_id)
        if conversation is None or str(conversation.user_id) != payload.user_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="会话上下文不可用")

    # 3. Execute via orchestrator (does NOT call Dify router workflow).
    context_dicts = [{"role": m.role, "content": m.content} for m in payload.context] if payload.context else None
    result = await shared.workflow.run_callback(
        db,
        payload.query,
        context=context_dicts,
        conversation_id=payload.conversation_id,
        user_id=payload.user_id,
        route=payload.route,
        media_intent=payload.media_intent,
    )

    elapsed_ms = int((time.monotonic() - started) * 1000)
    # Structured log: request_id, route, elapsed, status only.
    logging.getLogger("business_ai.callback").info(
        "callback request_id=%s route=%s elapsed_ms=%d status=%s",
        payload.request_id,
        payload.route,
        elapsed_ms,
        "fallback" if result.used_fallback else "ok",
    )

    return LangGraphCallbackResponse(
        answer=result.answer,
        category=result.category,
        citations=result.citations,
        trace=result.trace,
        artifacts=result.artifacts,
        need_clarification=False,
        used_fallback=result.used_fallback,
    )


@router.post("/dify/customer-service", response_model=DifyWorkflowResponse, tags=["dify"])
async def run_dify_customer_service(
    payload: DifyWorkflowRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DifyWorkflowResponse:
    result = await shared.dify_gateway.run_customer_service(payload.query, str(current_user.id))
    if result.degraded or not result.answer:
        local_result = await shared.workflow.run(db, payload.query)
        detail = result.detail if result.degraded else "Dify 返回空回答，已执行本地回退"
        return DifyWorkflowResponse(
            answer=local_result.answer,
            mode="local_fallback",
            degraded=True,
            detail=detail,
            citations=local_result.citations,
            trace=[AgentTrace(step="Dify Gateway", status="fallback", detail=detail), *local_result.trace],
        )
    return DifyWorkflowResponse(
        answer=result.answer,
        mode=result.mode,
        degraded=result.degraded,
        detail=result.detail,
        citations=[],
        trace=[],
    )


def _dify_media_response(result) -> DifyMediaResponse:
    """Convert only verified remote media into a successful API response."""
    if (
        result.degraded
        or result.mode != "remote"
        or not result.output
        or not (result.media_url or result.data_url)
        or not result.content_type
    ):
        status_code = result.status_code if result.status_code in {502, 503} else 502
        raise HTTPException(status_code=status_code, detail=result.detail)
    return DifyMediaResponse(
        kind=result.kind,
        mode="remote",
        detail=result.detail,
        output=result.output,
        media_url=result.media_url,
        data_url=result.data_url,
        content_type=result.content_type,
        byte_size=result.byte_size,
    )


@router.post(
    "/dify/text-to-speech",
    response_model=DifyMediaResponse,
    tags=["dify"],
)
@router.post("/dify/tts", response_model=DifyMediaResponse, include_in_schema=False)
async def run_dify_text_to_speech(
    payload: DifyTextToSpeechRequest,
    current_user: User = Depends(get_current_user),
) -> DifyMediaResponse:
    if not payload.text.strip():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="文本不能为空")
    result = await shared.dify_gateway.run_text_to_speech(payload.text, payload.voice, str(current_user.id))
    return _dify_media_response(result)


@router.post(
    "/dify/media/proxy",
    response_class=Response,
    tags=["dify"],
)
async def proxy_dify_media(
    payload: DifyMediaProxyRequest,
    _: User = Depends(get_current_user),
) -> Response:
    """Return verified provider bytes for browser media elements.

    The proxy is request-scoped and never stores or synthesizes media.  It is
    needed because an ``<audio>`` element cannot send this app's bearer token,
    and because the published TTS provider can emit non-canonical WAV sizes.
    """
    try:
        media = await shared.dify_gateway.fetch_remote_media(payload.url, payload.kind)
    except DifyMediaProxyError as error:
        raise HTTPException(status_code=error.status_code, detail=error.detail) from error
    return Response(
        content=media.payload,
        media_type=media.content_type,
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post(
    "/dify/text-to-image",
    response_model=DifyMediaResponse,
    tags=["dify"],
)
@router.post("/dify/image", response_model=DifyMediaResponse, include_in_schema=False)
async def run_dify_text_to_image(
    payload: DifyTextToImageRequest,
    current_user: User = Depends(get_current_user),
) -> DifyMediaResponse:
    if not payload.prompt.strip():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="提示词不能为空")
    result = await shared.dify_gateway.run_text_to_image(payload.prompt, payload.size, str(current_user.id))
    return _dify_media_response(result)
