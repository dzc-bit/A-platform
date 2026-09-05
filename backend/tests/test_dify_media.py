from __future__ import annotations

import asyncio
import base64
import io
import struct
import wave
from dataclasses import replace

import pytest

from .conftest import login
from app.routers import shared as api_shared
from app.services import dify as dify_module
from app.services.dify import DifyFetchedMedia, DifyGateway, DifyMediaResult


class _FakeResponse:
    def __init__(self, payload: object, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise dify_module.httpx.HTTPStatusError(
                "upstream failure",
                request=dify_module.httpx.Request("POST", "https://dify.example/v1/workflows/run"),
                response=dify_module.httpx.Response(self.status_code),
            )

    def json(self) -> object:
        return self._payload


class _FakeAsyncClient:
    response: _FakeResponse
    calls: list[dict[str, object]] = []

    def __init__(self, *, timeout: int) -> None:
        self.timeout = timeout

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def post(self, endpoint: str, *, headers: dict[str, str], json: dict[str, object]) -> _FakeResponse:
        self.calls.append({"endpoint": endpoint, "headers": headers, "json": json})
        return self.response


def _remote_media(
    kind: str,
    *,
    media_url: str | None = None,
    data_url: str | None = None,
) -> DifyMediaResult:
    return DifyMediaResult(
        kind=kind,  # type: ignore[arg-type]
        output={kind: media_url or data_url},
        media_url=media_url,
        data_url=data_url,
        content_type="audio/mpeg" if kind == "audio" else "image/png",
        byte_size=4 if data_url else None,
        mode="remote",
        degraded=False,
        detail="Dify 工作流调用成功",
        status_code=200,
    )


def test_gateway_posts_tts_inputs_to_dify_and_uses_specific_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.response = _FakeResponse(
        {"data": {"status": "succeeded", "outputs": {"audio_url": "https://media.example/voice.mp3"}}}
    )
    monkeypatch.setattr(dify_module.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(
        dify_module,
        "settings",
        replace(
            dify_module.settings,
            dify_api_url="https://dify.example/v1",
            dify_api_key="app-customer",
            dify_tts_api_key="app-tts",
            dify_media_allowed_hosts=(),
        ),
    )

    result = asyncio.run(DifyGateway().run_text_to_speech("已审核答复", "female", "7"))

    assert result.degraded is False
    assert result.media_url == "https://media.example/voice.mp3"
    call = _FakeAsyncClient.calls[0]
    assert call["endpoint"] == "https://dify.example/v1/workflows/run"
    assert call["headers"]["Authorization"] == "Bearer app-tts"  # type: ignore[index]
    assert call["json"] == {  # type: ignore[comparison-overlap]
        "inputs": {"text": "已审核答复", "voice": "female"},
        "response_mode": "blocking",
        "user": "7",
    }


def test_media_workflow_key_falls_back_to_customer_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.response = _FakeResponse(
        {"data": {"outputs": {"image_url": "https://media.example/render.png"}}}
    )
    monkeypatch.setattr(dify_module.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(
        dify_module,
        "settings",
        replace(
            dify_module.settings,
            dify_api_url="https://dify.example",
            dify_api_key="app-shared",
            dify_image_api_key="",
            dify_media_allowed_hosts=(),
        ),
    )

    result = asyncio.run(DifyGateway().run_text_to_image("商务素材", "1024x1024", "7"))

    assert result.degraded is False
    assert _FakeAsyncClient.calls[0]["headers"]["Authorization"] == "Bearer app-shared"  # type: ignore[index]


def test_image_gateway_normalises_legacy_size_for_qwen_workflow(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.response = _FakeResponse(
        {"data": {"outputs": {"image_url": "https://media.example/render.png"}}}
    )
    monkeypatch.setattr(dify_module.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(
        dify_module,
        "settings",
        replace(
            dify_module.settings,
            dify_api_url="https://dify.example",
            dify_api_key="app-image",
            dify_image_api_key="app-image",
            dify_media_allowed_hosts=(),
        ),
    )

    result = asyncio.run(DifyGateway().run_text_to_image("商务素材", "1024x1024", "7"))

    assert result.degraded is False
    assert _FakeAsyncClient.calls[0]["json"]["inputs"]["size"] == "2048*2048"  # type: ignore[index]


def test_media_gateway_rejects_text_that_only_claims_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeAsyncClient.response = _FakeResponse(
        {"data": {"status": "succeeded", "outputs": {"audio": "生成成功"}}}
    )
    monkeypatch.setattr(dify_module.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(
        dify_module,
        "settings",
        replace(dify_module.settings, dify_api_url="https://dify.example", dify_api_key="app-media"),
    )

    result = asyncio.run(DifyGateway().run_text_to_speech("答复", "default", "7"))

    assert result.degraded is True
    assert result.status_code == 502
    assert result.media_url is None
    assert result.data_url is None


@pytest.mark.parametrize(
    ("kind", "outputs"),
    (
        ("audio", {"audio_url": "https://media.example/placeholder.mp3"}),
        ("audio", {"audio_url": "https://media.example/placeholder"}),
        ("audio", {"task_id": "tts-task-123"}),
        ("image", {"image_url": "https://media.example/placeholder.png"}),
        ("image", {"task_id": "image-task-123"}),
    ),
)
def test_media_gateway_rejects_placeholder_urls_and_task_ids(
    kind: str,
    outputs: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A workflow task/status value or placeholder URL is not media evidence."""
    _FakeAsyncClient.response = _FakeResponse({"data": {"status": "succeeded", "outputs": outputs}})
    monkeypatch.setattr(dify_module.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(
        dify_module,
        "settings",
        replace(dify_module.settings, dify_api_url="https://dify.example", dify_api_key="app-media"),
    )

    if kind == "audio":
        result = asyncio.run(DifyGateway().run_text_to_speech("答复", "default", "7"))
    else:
        result = asyncio.run(DifyGateway().run_text_to_image("商务素材", "1024x1024", "7"))

    assert result.degraded is True
    assert result.status_code == 502
    assert result.media_url is None
    assert result.data_url is None


@pytest.mark.parametrize(
    ("kind", "output"),
    (
        ("audio", {"audio": "data:audio/mpeg;base64,bm90IGF1ZGlv"}),
        ("audio", {"audio_base64": base64.b64encode(b"not an audio payload").decode("ascii")}),
        ("image", {"image": "data:image/png;base64,iVBORw0KGgo="}),
    ),
)
def test_media_gateway_rejects_mislabeled_or_too_short_media_payloads(
    kind: str,
    output: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A declared MIME or an eight-byte magic header is not media evidence."""
    _FakeAsyncClient.response = _FakeResponse({"data": {"outputs": output}})
    monkeypatch.setattr(dify_module.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(
        dify_module,
        "settings",
        replace(dify_module.settings, dify_api_url="https://dify.example", dify_api_key="app-media"),
    )

    if kind == "audio":
        result = asyncio.run(DifyGateway().run_text_to_speech("答复", "default", "7"))
    else:
        result = asyncio.run(DifyGateway().run_text_to_image("商务素材", "1024x1024", "7"))

    assert result.degraded is True
    assert result.status_code == 502
    assert result.media_url is None
    assert result.data_url is None


def test_media_gateway_accepts_structurally_valid_png_and_wav_data(monkeypatch: pytest.MonkeyPatch) -> None:
    png = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    wav_buffer = io.BytesIO()
    with wave.open(wav_buffer, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(b"\x00\x00")
    wav = base64.b64encode(wav_buffer.getvalue()).decode("ascii")

    monkeypatch.setattr(dify_module.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(
        dify_module,
        "settings",
        replace(dify_module.settings, dify_api_url="https://dify.example", dify_api_key="app-media"),
    )
    _FakeAsyncClient.response = _FakeResponse(
        {"data": {"outputs": {"audio_base64": wav}}}
    )
    audio = asyncio.run(DifyGateway().run_text_to_speech("答复", "default", "7"))
    assert audio.degraded is False
    assert audio.content_type == "audio/wav"
    assert audio.byte_size == len(wav_buffer.getvalue())
    assert audio.data_url and audio.data_url.startswith("data:audio/wav;base64,")

    _FakeAsyncClient.response = _FakeResponse(
        {"data": {"outputs": {"image": f"data:image/png;base64,{png}"}}}
    )
    image = asyncio.run(DifyGateway().run_text_to_image("商务素材", "1024x1024", "7"))
    assert image.degraded is False
    assert image.content_type == "image/png"


def test_wav_proxy_normalises_provider_placeholder_sizes() -> None:
    """Browser playback must not depend on desktop decoders tolerating bad sizes."""
    source = io.BytesIO()
    with wave.open(source, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(24_000)
        stream.writeframes(b"\x00\x00" * 32)
    malformed = bytearray(source.getvalue())
    struct.pack_into("<I", malformed, 4, 0x7FFFFFFF)
    struct.pack_into("<I", malformed, 40, 0x7FFFFFFF)

    repaired = dify_module._normalise_wav(bytes(malformed))

    assert repaired is not None
    assert struct.unpack_from("<I", repaired, 4)[0] == len(repaired) - 8
    assert struct.unpack_from("<I", repaired, 40)[0] == len(repaired) - 44
    with wave.open(io.BytesIO(repaired), "rb") as stream:
        assert stream.getnchannels() == 1
        assert stream.getframerate() == 24_000
        assert stream.getnframes() == 32


def test_media_proxy_fetches_and_repairs_real_remote_wav(monkeypatch: pytest.MonkeyPatch) -> None:
    source = io.BytesIO()
    with wave.open(source, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(24_000)
        # PCM can coincidentally contain an MP3-looking frame header.  A file
        # with an explicit RIFF/WAVE signature must still stay on the WAV
        # validation and repair path.
        stream.writeframes(b"\xff\xfb\x90\x64" + b"\x01\x00" * 510)
    malformed = bytearray(source.getvalue())
    struct.pack_into("<I", malformed, 4, 0x7FFFFFFF)
    struct.pack_into("<I", malformed, 40, 0x7FFFFFFF)

    class _ProxyResponse:
        status_code = 200
        headers = {"content-type": "audio/x-wav", "content-length": str(len(malformed))}
        content = bytes(malformed)

    class _ProxyClient:
        def __init__(self, *, timeout: int, follow_redirects: bool) -> None:
            assert timeout == 60
            assert follow_redirects is False

        async def __aenter__(self) -> "_ProxyClient":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(self, url: str, *, headers: dict[str, str]) -> _ProxyResponse:
            assert url == "https://media.example/audio.wav"
            assert headers == {"Accept": "audio/*"}
            return _ProxyResponse()

    monkeypatch.setattr(dify_module.httpx, "AsyncClient", _ProxyClient)
    monkeypatch.setattr(
        dify_module,
        "settings",
        replace(dify_module.settings, dify_media_allowed_hosts=()),
    )

    media = asyncio.run(DifyGateway().fetch_remote_media("https://media.example/audio.wav", "audio"))

    assert media.content_type == "audio/wav"
    with wave.open(io.BytesIO(media.payload), "rb") as stream:
        assert stream.getnframes() == 512


def test_media_proxy_endpoint_returns_verified_bytes(client, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = b"RIFF\x00\x00\x00\x00WAVE" + b"verified provider bytes"

    async def fake_fetch(url: str, kind: str) -> DifyFetchedMedia:
        assert (url, kind) == ("https://media.example/audio.wav", "audio")
        return DifyFetchedMedia(payload=payload, content_type="audio/wav")

    monkeypatch.setattr(api_shared.dify_gateway, "fetch_remote_media", fake_fetch)
    response = client.post(
        "/api/v1/dify/media/proxy",
        headers=login(client),
        json={"url": "https://media.example/audio.wav", "kind": "audio"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.headers["cache-control"] == "no-store"
    assert response.content == payload


def test_media_gateway_reports_missing_credentials_without_calling_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeAsyncClient.calls = []
    monkeypatch.setattr(dify_module.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(
        dify_module,
        "settings",
        replace(dify_module.settings, dify_api_url="", dify_api_key="", dify_tts_api_key=""),
    )

    result = asyncio.run(DifyGateway().run_text_to_speech("答复", "default", "7"))

    assert result.degraded is True
    assert result.status_code == 503
    assert _FakeAsyncClient.calls == []


def test_media_gateway_reports_upstream_http_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.response = _FakeResponse({"error": "upstream"}, status_code=502)
    monkeypatch.setattr(dify_module.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(
        dify_module,
        "settings",
        replace(dify_module.settings, dify_api_url="https://dify.example", dify_api_key="app-media"),
    )

    result = asyncio.run(DifyGateway().run_text_to_image("商务素材", "1024x1024", "7"))

    assert result.degraded is True
    assert result.status_code == 502
    assert "HTTPStatusError" in result.detail


def test_tts_endpoint_returns_verified_external_url(client, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_tts(text: str, voice: str, user: str) -> DifyMediaResult:
        assert (text, voice, user) == ("请稍候", "default", "1")
        return _remote_media("audio", media_url="https://media.example/signed/audio")

    monkeypatch.setattr(api_shared.dify_gateway, "run_text_to_speech", fake_tts)
    response = client.post(
        "/api/v1/dify/text-to-speech",
        headers=login(client),
        json={"text": "请稍候"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "remote"
    assert payload["media_url"] == "https://media.example/signed/audio"
    assert payload["data_url"] is None
    assert payload["content_type"].startswith("audio/")


def test_image_endpoint_returns_verified_data_url(client, monkeypatch: pytest.MonkeyPatch) -> None:
    # A complete 1x1 PNG (not just the eight-byte signature) keeps this API
    # contract test from treating a magic prefix as a rendered image.
    png_data_url = (
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )

    async def fake_image(prompt: str, size: str, user: str) -> DifyMediaResult:
        assert (prompt, size, user) == ("商务办公室", "1024x1024", "1")
        return _remote_media("image", data_url=png_data_url)

    monkeypatch.setattr(api_shared.dify_gateway, "run_text_to_image", fake_image)
    response = client.post(
        "/api/v1/dify/text-to-image",
        headers=login(client),
        json={"prompt": "商务办公室"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["data_url"] == png_data_url
    assert payload["media_url"] is None
    assert payload["kind"] == "image"


def test_media_endpoint_does_not_fabricate_success_when_dify_is_unavailable(client, monkeypatch) -> None:
    async def unavailable(*_args: object, **_kwargs: object) -> DifyMediaResult:
        return DifyMediaResult(
            kind="image",
            output=None,
            media_url=None,
            data_url=None,
            content_type=None,
            byte_size=None,
            mode="unavailable",
            degraded=True,
            detail="未配置 DIFY_API_URL 或对应工作流 API Key",
            status_code=503,
        )

    monkeypatch.setattr(api_shared.dify_gateway, "run_text_to_image", unavailable)
    response = client.post(
        "/api/v1/dify/image",
        headers=login(client),
        json={"prompt": "商务素材"},
    )

    assert response.status_code == 503
    assert "未配置" in response.json()["detail"]
