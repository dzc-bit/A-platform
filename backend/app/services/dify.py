from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import mimetypes
import struct
import zlib
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import unquote_to_bytes, urljoin, urlparse

import httpx

from ..config import settings


MAX_MEDIA_BYTES = 20 * 1024 * 1024

# Keep the public API's historic ``1024x1024``-style values usable while the
# Qwen-Image workflow uses DashScope's ``width*height`` notation.
_IMAGE_SIZE_ALIASES = {
    "1024x1024": "2048*2048",
    "1280x720": "2688*1536",
    "720x1280": "1536*2688",
}


def _normalise_image_size(size: str) -> str:
    value = (size or "").strip()
    return _IMAGE_SIZE_ALIASES.get(value, value)


@dataclass(frozen=True)
class DifyWorkflowResult:
    """Result of a published Dify workflow call.

    ``outputs`` is kept separate from ``answer`` so media workflows can be
    validated without coercing an external response into a fabricated string.
    The extra fields have defaults to preserve the customer-service API used by
    existing callers and tests.
    """

    answer: str | None
    mode: str
    degraded: bool
    detail: str
    outputs: dict[str, Any] | None = None
    status_code: int = 502


@dataclass(frozen=True)
class DifyMediaResult:
    """A verified reference returned by an external media workflow.

    A successful result always contains either ``media_url`` or ``data_url``.
    There is intentionally no local fallback: returning a success status for a
    generated placeholder would make a media workflow appear to have passed.
    """

    kind: Literal["audio", "image"]
    output: dict[str, Any] | None
    media_url: str | None
    data_url: str | None
    content_type: str | None
    byte_size: int | None
    mode: str
    degraded: bool
    detail: str
    status_code: int = 502


@dataclass(frozen=True)
class DifyFetchedMedia:
    """A validated media payload fetched from a published workflow URL.

    The payload is held only for the duration of the proxy request.  It is
    never written to the course application's storage, so playback still uses
    the real provider response rather than a generated or static substitute.
    """

    payload: bytes
    content_type: str


class DifyMediaProxyError(RuntimeError):
    """An external media URL could not be fetched or validated for playback."""

    def __init__(self, detail: str, status_code: int = 502) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


@dataclass(frozen=True)
class _MediaEvidence:
    media_url: str | None
    data_url: str | None
    content_type: str
    byte_size: int | None


# A workflow must return a provider media reference, not a demo/status URL.
# Reserved example hosts and explicit placeholder markers are never valid media
# sources, even when their path happens to end in ``.mp3``/``.png``.
_PLACEHOLDER_HOSTS = frozenset({"example.com", "example.org", "example.net", "invalid", "test"})
_PLACEHOLDER_MARKERS = ("placeholder", "dummy", "fake", "mock")
_TASK_QUERY_MARKERS = ("task_id", "taskid", "job_id", "jobid")


def _expected_prefix(kind: Literal["audio", "image"]) -> str:
    return f"{kind}/"


def _normalise_mime(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    mime = value.split(";", maxsplit=1)[0].strip().lower()
    return mime or None


def _mime_matches(mime: str | None, kind: Literal["audio", "image"]) -> bool:
    return bool(mime and mime.startswith(_expected_prefix(kind)))


def _mime_compatible(declared: str | None, sniffed: str) -> bool:
    """Allow common provider aliases while still checking the actual format."""
    if not declared:
        return True
    aliases = {
        "image/jpg": "image/jpeg",
        "audio/mp3": "audio/mpeg",
        "audio/x-mp3": "audio/mpeg",
        "audio/x-wav": "audio/wav",
        "audio/wave": "audio/wav",
        "audio/x-flac": "audio/flac",
    }
    return aliases.get(declared, declared) == aliases.get(sniffed, sniffed)


def _valid_png(payload: bytes) -> bool:
    if len(payload) < 57 or not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return False
    offset = 8
    seen_ihdr = seen_idat = False
    while offset + 12 <= len(payload):
        length = struct.unpack(">I", payload[offset : offset + 4])[0]
        end = offset + 12 + length
        if length > MAX_MEDIA_BYTES or end > len(payload):
            return False
        chunk_type = payload[offset + 4 : offset + 8]
        chunk = payload[offset + 8 : offset + 8 + length]
        checksum = struct.unpack(">I", payload[offset + 8 + length : end])[0]
        if zlib.crc32(chunk_type + chunk) & 0xFFFFFFFF != checksum:
            return False
        if not seen_ihdr:
            if chunk_type != b"IHDR" or length != 13:
                return False
            width, height, depth, colour, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", chunk
            )
            valid_depths = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8}, 4: {8, 16}, 6: {8, 16}}
            if (
                not width
                or not height
                or width > 100_000
                or height > 100_000
                or depth not in valid_depths.get(colour, set())
                or compression != 0
                or filtering != 0
                or interlace not in {0, 1}
            ):
                return False
            seen_ihdr = True
        elif chunk_type == b"IDAT" and length:
            seen_idat = True
        elif chunk_type == b"IEND":
            return length == 0 and seen_idat and end == len(payload)
        offset = end
    return False


def _valid_jpeg(payload: bytes) -> bool:
    if len(payload) < 32 or not payload.startswith(b"\xff\xd8") or not payload.endswith(b"\xff\xd9"):
        return False
    sof_markers = {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
    offset = 2
    seen_sof = False
    while offset + 1 < len(payload):
        if payload[offset] != 0xFF:
            return False
        while offset < len(payload) and payload[offset] == 0xFF:
            offset += 1
        if offset >= len(payload):
            return False
        marker = payload[offset]
        offset += 1
        if marker == 0xD9:
            return seen_sof
        if marker == 0xDA:
            if offset + 2 > len(payload):
                return False
            segment_length = struct.unpack(">H", payload[offset : offset + 2])[0]
            if segment_length < 2 or offset + segment_length > len(payload):
                return False
            return seen_sof and offset + segment_length < len(payload)
        if marker in {0x01, *range(0xD0, 0xD9)}:
            continue
        if offset + 2 > len(payload):
            return False
        segment_length = struct.unpack(">H", payload[offset : offset + 2])[0]
        if segment_length < 2 or offset + segment_length > len(payload):
            return False
        if marker in sof_markers:
            if segment_length < 8:
                return False
            height, width = struct.unpack(">HH", payload[offset + 3 : offset + 7])
            if not width or not height or width > 100_000 or height > 100_000:
                return False
            seen_sof = True
        offset += segment_length
    return False


def _skip_gif_subblocks(payload: bytes, offset: int) -> int | None:
    while offset < len(payload):
        size = payload[offset]
        offset += 1
        if size == 0:
            return offset
        if offset + size > len(payload):
            return None
        offset += size
    return None


def _valid_gif(payload: bytes) -> bool:
    if len(payload) < 20 or not payload.startswith((b"GIF87a", b"GIF89a")) or payload[-1:] != b"\x3b":
        return False
    width, height = struct.unpack("<HH", payload[6:10])
    if not width or not height:
        return False
    offset = 13
    packed = payload[10]
    if packed & 0x80:
        offset += 3 * (2 ** ((packed & 0x07) + 1))
    seen_image = False
    while offset < len(payload):
        marker = payload[offset]
        if marker == 0x3B:
            return seen_image and offset == len(payload) - 1
        if marker == 0x21:
            if offset + 2 > len(payload):
                return False
            next_offset = _skip_gif_subblocks(payload, offset + 2)
            if next_offset is None:
                return False
            offset = next_offset
            continue
        if marker != 0x2C or offset + 10 > len(payload):
            return False
        image_width, image_height = struct.unpack("<HH", payload[offset + 5 : offset + 9])
        if not image_width or not image_height:
            return False
        local_packed = payload[offset + 9]
        offset += 10
        if local_packed & 0x80:
            offset += 3 * (2 ** ((local_packed & 0x07) + 1))
        if offset >= len(payload):
            return False
        offset += 1  # LZW minimum code size
        next_offset = _skip_gif_subblocks(payload, offset)
        if next_offset is None:
            return False
        offset = next_offset
        seen_image = True
    return False


def _valid_webp(payload: bytes) -> bool:
    if len(payload) < 30 or payload[:4] != b"RIFF" or payload[8:12] != b"WEBP":
        return False
    if struct.unpack("<I", payload[4:8])[0] + 8 != len(payload):
        return False
    offset = 12
    while offset + 8 <= len(payload):
        chunk_type = payload[offset : offset + 4]
        size = struct.unpack("<I", payload[offset + 4 : offset + 8])[0]
        end = offset + 8 + size
        if end > len(payload):
            return False
        chunk = payload[offset + 8 : end]
        if chunk_type == b"VP8X" and len(chunk) >= 10:
            width = 1 + int.from_bytes(chunk[4:7], "little")
            height = 1 + int.from_bytes(chunk[7:10], "little")
            if width and height:
                return True
        if chunk_type == b"VP8 " and len(chunk) >= 10 and chunk[3:6] == b"\x9d\x01\x2a":
            width, height = struct.unpack("<HH", chunk[6:10])
            if width & 0x3FFF and height & 0x3FFF:
                return True
        if chunk_type == b"VP8L" and len(chunk) >= 5 and chunk[0] == 0x2F:
            width = 1 + (chunk[1] | ((chunk[2] & 0x3F) << 8))
            height = 1 + (((chunk[2] >> 6) | (chunk[3] << 2) | ((chunk[4] & 0x0F) << 10)))
            if width and height:
                return True
        offset = end + (size & 1)
    return False


def _valid_wav(payload: bytes) -> bool:
    if len(payload) < 44 or payload[:4] != b"RIFF" or payload[8:12] != b"WAVE":
        return False
    riff_size = struct.unpack("<I", payload[4:8])[0]
    if riff_size + 8 > len(payload) or riff_size < 36:
        return False
    offset = 12
    seen_fmt = seen_data = False
    while offset + 8 <= len(payload):
        chunk_type = payload[offset : offset + 4]
        size = struct.unpack("<I", payload[offset + 4 : offset + 8])[0]
        end = offset + 8 + size
        if end > len(payload):
            return False
        chunk = payload[offset + 8 : end]
        if chunk_type == b"fmt ":
            if len(chunk) < 16:
                return False
            audio_format, channels, sample_rate, byte_rate, block_align, bits = struct.unpack(
                "<HHIIHH", chunk[:16]
            )
            if audio_format == 0 or not (1 <= channels <= 32) or not sample_rate or not byte_rate or not block_align or not bits:
                return False
            seen_fmt = True
        elif chunk_type == b"data" and size:
            seen_data = True
        offset = end + (size & 1)
    return seen_fmt and seen_data


def _normalise_wav(payload: bytes) -> bytes | None:
    """Repair provider WAV headers whose declared sizes exceed the payload.

    Some TTS providers return a valid PCM stream with placeholder 32-bit RIFF
    and ``data`` sizes (for example ``0x7ffffxxx``).  Desktop decoders may
    tolerate that, while browser media elements reject it.  We retain every
    byte from the provider and only rewrite container lengths to match the
    bytes that were actually fetched.
    """
    if len(payload) < 44 or payload[:4] != b"RIFF" or payload[8:12] != b"WAVE":
        return None

    normalised = bytearray(payload)
    # The application caps media at MAX_MEDIA_BYTES, so this always fits the
    # RIFF 32-bit size field.
    struct.pack_into("<I", normalised, 4, len(payload) - 8)

    offset = 12
    seen_fmt = False
    seen_data = False
    while offset + 8 <= len(payload):
        chunk_type = payload[offset : offset + 4]
        declared_size = struct.unpack("<I", payload[offset + 4 : offset + 8])[0]
        available_size = len(payload) - offset - 8

        # A malformed data chunk is the known DashScope/Dify response shape.
        # Treat all remaining provider bytes as audio data and make the chunk
        # self-consistent.  Other malformed chunks are rejected rather than
        # guessing how their structure should be repaired.
        if declared_size > available_size:
            if chunk_type != b"data" or available_size <= 0:
                return None
            struct.pack_into("<I", normalised, offset + 4, available_size)
            seen_data = True
            break

        chunk_end = offset + 8 + declared_size
        if chunk_end > len(payload):
            return None
        if chunk_type == b"fmt ":
            if declared_size < 16:
                return None
            seen_fmt = True
        elif chunk_type == b"data" and declared_size:
            seen_data = True

        # RIFF chunks are word aligned.  A missing pad byte is tolerated by
        # the existing validator; stop rather than stepping past the payload.
        offset = chunk_end + (declared_size & 1)

    if not seen_fmt or not seen_data:
        return None
    return bytes(normalised)


def _mp3_frame_length(payload: bytes, offset: int) -> int | None:
    if offset + 4 > len(payload) or payload[offset] != 0xFF:
        return None
    first, second, third = payload[offset + 1 : offset + 4]
    version = (first >> 3) & 0x03
    layer = (first >> 1) & 0x03
    bitrate_index = (second >> 4) & 0x0F
    sample_index = (second >> 2) & 0x03
    padding = (second >> 1) & 0x01
    if version == 1 or layer == 0 or bitrate_index in {0, 15} or sample_index == 3:
        return None
    sample_rates = [44100, 48000, 32000]
    sample_rate = sample_rates[sample_index]
    if version == 2:
        sample_rate //= 2
    elif version == 0:
        sample_rate //= 4
    bitrate_tables = {
        (3, 3): [0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448],
        (3, 2): [0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384],
        (3, 1): [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
        (2, 3): [0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256],
        (2, 2): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
        (2, 1): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
        (0, 3): [0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256],
        (0, 2): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
        (0, 1): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
    }
    bitrate = bitrate_tables[(version, layer)][bitrate_index] * 1000
    if layer == 3:
        return (12 * bitrate // sample_rate + padding) * 4
    coefficient = 72 if version != 3 else 144
    return coefficient * bitrate // sample_rate + padding


def _valid_mp3(payload: bytes) -> bool:
    offset = 0
    if payload.startswith(b"ID3"):
        if len(payload) < 10 or payload[3] not in {2, 3, 4}:
            return False
        if any(byte & 0x80 for byte in payload[6:10]):
            return False
        tag_size = sum(payload[index] << (7 * (3 - index)) for index in range(6, 10))
        offset = 10 + tag_size + (10 if payload[5] & 0x10 else 0)
    for candidate in range(offset, min(len(payload) - 4, offset + 4096)):
        frame_length = _mp3_frame_length(payload, candidate)
        if frame_length is None or candidate + frame_length > len(payload) or frame_length < 24:
            continue
        if any(payload[candidate + 4 : candidate + frame_length]):
            return True
    return False


def _valid_adts(payload: bytes) -> bool:
    if len(payload) < 9 or payload[0] != 0xFF or payload[1] & 0xF6 != 0xF0:
        return False
    sample_index = (payload[2] >> 2) & 0x0F
    channel_config = ((payload[2] & 0x01) << 2) | ((payload[3] >> 6) & 0x03)
    frame_length = ((payload[3] & 0x03) << 11) | (payload[4] << 3) | ((payload[5] >> 5) & 0x07)
    header_length = 9 if payload[1] & 0x01 == 0 else 7
    return sample_index != 15 and channel_config != 0 and frame_length >= header_length and frame_length <= len(payload) and any(payload[header_length:frame_length])


def _valid_ogg(payload: bytes) -> bool:
    if len(payload) < 27 or payload[:4] != b"OggS" or payload[4] != 0:
        return False
    segment_count = payload[26]
    table_end = 27 + segment_count
    if table_end > len(payload) or not segment_count:
        return False
    body_length = sum(payload[27:table_end])
    body_end = table_end + body_length
    if body_end > len(payload) or body_length < 16:
        return False
    body = payload[table_end:body_end]
    return (body.startswith(b"OpusHead") and len(body) >= 19) or (body.startswith(b"\x01vorbis") and len(body) >= 30)


def _valid_flac(payload: bytes) -> bool:
    if len(payload) < 46 or not payload.startswith(b"fLaC"):
        return False
    offset = 4
    streaminfo = False
    last_block = False
    while offset + 4 <= len(payload):
        header = payload[offset]
        block_type = header & 0x7F
        last_block = bool(header & 0x80)
        size = int.from_bytes(payload[offset + 1 : offset + 4], "big")
        end = offset + 4 + size
        if end > len(payload):
            return False
        if block_type == 0:
            if size != 34 or streaminfo:
                return False
            packed = int.from_bytes(payload[offset + 14 : offset + 22], "big")
            sample_rate = packed >> 44
            channels = ((packed >> 41) & 0x07) + 1
            bits = ((packed >> 36) & 0x1F) + 1
            if not sample_rate or not channels or not bits:
                return False
            streaminfo = True
        offset = end
        if last_block:
            break
    if not streaminfo or not last_block:
        return False
    return any(payload[index] == 0xFF and index + 1 < len(payload) and payload[index + 1] & 0xFC == 0xF8 for index in range(offset, len(payload) - 1))


def _valid_mp4_audio(payload: bytes) -> bool:
    if len(payload) < 32 or payload[4:8] != b"ftyp":
        return False
    offset = 0
    seen_ftyp = seen_moov = seen_mdat = False
    while offset + 8 <= len(payload):
        size = struct.unpack(">I", payload[offset : offset + 4])[0]
        box_type = payload[offset + 4 : offset + 8]
        header = 8
        if size == 1:
            if offset + 16 > len(payload):
                return False
            size = struct.unpack(">Q", payload[offset + 8 : offset + 16])[0]
            header = 16
        elif size == 0:
            size = len(payload) - offset
        if size < header or offset + size > len(payload):
            return False
        if box_type == b"ftyp":
            seen_ftyp = True
        elif box_type == b"moov" and size > header:
            seen_moov = True
        elif box_type == b"mdat" and size > header:
            seen_mdat = True
        offset += size
    return seen_ftyp and seen_moov and seen_mdat and offset == len(payload)


def _valid_webm(payload: bytes) -> bool:
    return len(payload) >= 64 and payload.startswith(b"\x1a\x45\xdf\xa3") and b"webm" in payload[:128].lower() and b"\x1f\x43\xb6\x75" in payload[4:]


def _valid_amr(payload: bytes) -> bool:
    if payload.startswith(b"#!AMR\n"):
        offset = 6
        sizes = (13, 14, 16, 18, 20, 21, 27, 32, 6, 0, 0, 0, 0, 0, 0, 0)
    elif payload.startswith(b"#!AMR-WB\n"):
        offset = 9
        sizes = (18, 24, 33, 37, 41, 47, 51, 59, 61, 0, 0, 0, 0, 0, 0, 0, 0)
    else:
        return False
    if offset >= len(payload):
        return False
    frame_type = (payload[offset] >> 3) & 0x0F
    return sizes[frame_type] > 0 and offset + 1 + sizes[frame_type] <= len(payload)


def _sniff_media_type(payload: bytes, kind: Literal["audio", "image"]) -> str | None:
    """Recognise a complete, minimally structured common media container."""
    if kind == "image":
        if payload.startswith(b"\x89PNG") and _valid_png(payload):
            return "image/png"
        if payload.startswith(b"\xff\xd8") and _valid_jpeg(payload):
            return "image/jpeg"
        if payload.startswith((b"GIF87a", b"GIF89a")) and _valid_gif(payload):
            return "image/gif"
        if payload.startswith(b"RIFF") and _valid_webp(payload):
            return "image/webp"
        return None

    if payload.startswith(b"RIFF") and _valid_wav(payload):
        return "audio/wav"
    if _valid_adts(payload):
        return "audio/aac"
    if _valid_mp3(payload):
        return "audio/mpeg"
    if payload.startswith(b"OggS") and _valid_ogg(payload):
        return "audio/ogg"
    if payload.startswith(b"fLaC") and _valid_flac(payload):
        return "audio/flac"
    if payload[4:8] == b"ftyp" and _valid_mp4_audio(payload):
        return "audio/mp4"
    if _valid_webm(payload):
        return "audio/webm"
    if _valid_amr(payload):
        return "audio/amr"
    return None


def _safe_media_host(host: str) -> bool:
    host = host.rstrip(".").lower()
    if not host or host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def _host_allowed(host: str) -> bool:
    allowed = settings.dify_media_allowed_hosts
    if not allowed:
        return True
    host = host.rstrip(".").lower()
    return any(host == item or host.endswith(f".{item}") for item in allowed)


def _url_evidence(
    value: str,
    kind: Literal["audio", "image"],
    key_path: str,
    hinted_mime: str | None,
) -> _MediaEvidence | None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username or parsed.password or not _safe_media_host(parsed.hostname):
        return None
    if not _host_allowed(parsed.hostname):
        return None

    host = parsed.hostname.rstrip(".").lower()
    path_and_query = f"{parsed.path}?{parsed.query}".casefold()
    if host in _PLACEHOLDER_HOSTS or any(host.endswith(f".{reserved}") for reserved in _PLACEHOLDER_HOSTS):
        return None
    if any(marker in path_and_query for marker in _PLACEHOLDER_MARKERS):
        return None
    if any(marker in parsed.query.casefold() for marker in _TASK_QUERY_MARKERS):
        return None

    mime = hinted_mime
    if not mime:
        mime = mimetypes.guess_type(parsed.path)[0]
    # A field name alone is not evidence: ``audio_url`` can contain a task ID,
    # HTML page, or placeholder.  Keep extension-less signed URLs usable only
    # when the workflow also supplies an explicit compatible MIME type.
    if not mime or mime.endswith("/*"):
        return None
    if mime and not _mime_matches(mime, kind):
        return None
    return _MediaEvidence(media_url=value, data_url=None, content_type=mime, byte_size=None)


def _data_evidence(
    value: str,
    kind: Literal["audio", "image"],
    hinted_mime: str | None,
) -> _MediaEvidence | None:
    if not value.startswith("data:") or "," not in value:
        return None
    metadata, encoded = value[5:].split(",", maxsplit=1)
    metadata_parts = metadata.split(";")
    mime = _normalise_mime(metadata_parts[0])
    if not _mime_matches(mime, kind):
        return None
    try:
        if "base64" in metadata_parts[1:]:
            payload = base64.b64decode(encoded, validate=True)
        else:
            payload = unquote_to_bytes(encoded)
    except (ValueError, binascii.Error):
        return None
    if not payload or len(payload) > MAX_MEDIA_BYTES:
        return None
    sniffed = _sniff_media_type(payload, kind)
    if sniffed is None or not _mime_compatible(mime, sniffed):
        return None
    return _MediaEvidence(
        media_url=None,
        data_url=value,
        content_type=sniffed,
        byte_size=len(payload),
    )


def _base64_evidence(
    value: str,
    kind: Literal["audio", "image"],
    key_path: str,
    hinted_mime: str | None,
) -> _MediaEvidence | None:
    lowered = key_path.lower()
    if not ("base64" in lowered or lowered.endswith(".data") or lowered in {"data", "body"}):
        return None
    if len(value) < 16 or len(value) > (MAX_MEDIA_BYTES * 4 // 3 + 4096):
        return None
    try:
        payload = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        return None
    if not payload or len(payload) > MAX_MEDIA_BYTES:
        return None
    sniffed = _sniff_media_type(payload, kind)
    if sniffed is None or (hinted_mime and not _mime_compatible(hinted_mime, sniffed)):
        return None
    data_url = f"data:{sniffed};base64,{value}"
    return _MediaEvidence(media_url=None, data_url=data_url, content_type=sniffed, byte_size=len(payload))


def _find_media_evidence(
    value: object,
    kind: Literal["audio", "image"],
    key_path: str = "",
    hinted_mime: str | None = None,
) -> _MediaEvidence | None:
    if isinstance(value, dict):
        local_hint = hinted_mime
        for key in ("content_type", "content-type", "mime_type", "mime", "media_type"):
            candidate = _normalise_mime(value.get(key))
            if candidate:
                local_hint = candidate
                break
        # Prefer explicit media fields so a status message or unrelated URL
        # cannot accidentally be accepted as a successful media result.
        keys = list(value)
        keys.sort(key=lambda item: (0 if kind in str(item).lower() else 1, str(item)))
        for key in keys:
            if str(key).lower() in {"content_type", "content-type", "mime_type", "mime", "media_type"}:
                continue
            child_path = f"{key_path}.{key}" if key_path else str(key)
            found = _find_media_evidence(value[key], kind, child_path, local_hint)
            if found:
                return found
        return None

    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found = _find_media_evidence(item, kind, f"{key_path}[{index}]", hinted_mime)
            if found:
                return found
        return None

    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate:
        return None

    # Some HTTP nodes return the provider JSON as a string body.  Parse only
    # object/array-shaped strings; ordinary text is never treated as media.
    if candidate[:1] in {"{", "["}:
        try:
            parsed = json.loads(candidate)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = None
        if parsed is not None:
            found = _find_media_evidence(parsed, kind, key_path, hinted_mime)
            if found:
                return found

    if candidate.startswith("data:"):
        return _data_evidence(candidate, kind, hinted_mime)
    if candidate.startswith(("http://", "https://")):
        return _url_evidence(candidate, kind, key_path, hinted_mime)
    return _base64_evidence(candidate, kind, key_path, hinted_mime)


class DifyGateway:
    """Call published Dify workflows and preserve explicit external failures."""

    @staticmethod
    def _endpoint() -> str:
        base = (settings.dify_api_url or "").strip().rstrip("/")
        # Be forgiving when a private deployment stores the optional /v1 path
        # in its root URL; the public configuration still documents the root.
        if base.endswith("/v1"):
            base = base[:-3].rstrip("/")
        return f"{base}/v1/workflows/run"

    async def _call_workflow(
        self,
        inputs: dict[str, Any],
        user: str,
        api_key: str | None = None,
    ) -> DifyWorkflowResult:
        key = (api_key or settings.dify_api_key or "").strip()
        if not settings.dify_api_url or not key:
            return DifyWorkflowResult(
                answer=None,
                mode="local_fallback",
                degraded=True,
                detail="未配置 DIFY_API_URL 或对应工作流 API Key",
                status_code=503,
            )
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                response = await client.post(
                    self._endpoint(),
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={"inputs": inputs, "response_mode": "blocking", "user": user},
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("Dify response is not an object")
                data = payload.get("data", payload)
                if not isinstance(data, dict):
                    raise ValueError("Dify response data is not an object")
                if str(data.get("status", "")).lower() in {"failed", "error"}:
                    raise ValueError("Dify workflow reported failure")
                outputs = data.get("outputs", data.get("output"))
                if not isinstance(outputs, dict):
                    raise ValueError("Dify response missing outputs")
                return DifyWorkflowResult(
                    answer=None,
                    mode="remote",
                    degraded=False,
                    detail="Dify 工作流调用成功",
                    outputs=outputs,
                    status_code=200,
                )
        except (httpx.HTTPError, TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
            return DifyWorkflowResult(
                answer=None,
                mode="local_fallback",
                degraded=True,
                detail=f"远程 Dify 调用失败：{type(error).__name__}",
                status_code=502,
            )

    async def run_customer_service(self, query: str, user: str) -> DifyWorkflowResult:
        result = await self._call_workflow({"query": query}, user)
        if result.degraded or not result.outputs:
            return result
        answer = result.outputs.get("answer") or result.outputs.get("text")
        if not isinstance(answer, str) or not answer.strip():
            return DifyWorkflowResult(
                answer=None,
                mode="local_fallback",
                degraded=True,
                detail="Dify response missing output text",
                outputs=result.outputs,
                status_code=502,
            )
        return DifyWorkflowResult(
            answer=answer.strip(),
            mode="remote",
            degraded=False,
            detail=result.detail,
            outputs=result.outputs,
            status_code=200,
        )

    async def _run_media(
        self,
        kind: Literal["audio", "image"],
        inputs: dict[str, Any],
        user: str,
        api_key: str | None,
    ) -> DifyMediaResult:
        result = await self._call_workflow(inputs, user, api_key=api_key)
        if result.degraded or result.outputs is None:
            return DifyMediaResult(
                kind=kind,
                output=None,
                media_url=None,
                data_url=None,
                content_type=None,
                byte_size=None,
                mode="unavailable",
                degraded=True,
                detail=result.detail,
                status_code=result.status_code,
            )
        evidence = _find_media_evidence(result.outputs, kind)
        if evidence is None:
            return DifyMediaResult(
                kind=kind,
                output=result.outputs,
                media_url=None,
                data_url=None,
                content_type=None,
                byte_size=None,
                mode="invalid_response",
                degraded=True,
                detail=f"Dify {kind} 工作流未返回可验证的真实媒体 URL 或数据",
                status_code=502,
            )
        return DifyMediaResult(
            kind=kind,
            output=result.outputs,
            media_url=evidence.media_url,
            data_url=evidence.data_url,
            content_type=evidence.content_type,
            byte_size=evidence.byte_size,
            mode="remote",
            degraded=False,
            detail=result.detail,
            status_code=200,
        )

    async def fetch_remote_media(
        self,
        media_url: str,
        kind: Literal["audio", "image"],
    ) -> DifyFetchedMedia:
        """Fetch and validate a media URL for browser playback.

        Dify's workflow response is intentionally kept as a remote reference,
        but browsers cannot attach the API bearer token to an ``<audio>`` tag
        and some provider WAV headers are not browser-compatible.  This
        short-lived proxy fetches the provider bytes, repairs only a malformed
        WAV container, and returns the verified bytes to the authenticated
        caller.
        """
        current_url = (media_url or "").strip()
        if _url_evidence(current_url, kind, f"{kind}_url", None) is None:
            raise DifyMediaProxyError("媒体 URL 不被允许", status_code=400)

        response = None
        try:
            async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
                for _ in range(4):
                    response = await client.get(
                        current_url,
                        headers={"Accept": f"{kind}/*"},
                    )
                    if response.status_code not in {301, 302, 303, 307, 308}:
                        break
                    location = response.headers.get("location", "")
                    next_url = urljoin(current_url, location)
                    if _url_evidence(next_url, kind, f"{kind}_url", None) is None:
                        raise DifyMediaProxyError("媒体重定向地址不被允许", status_code=502)
                    current_url = next_url
                else:
                    raise DifyMediaProxyError("媒体地址重定向次数过多", status_code=502)
        except DifyMediaProxyError:
            raise
        except httpx.HTTPError as error:
            raise DifyMediaProxyError(
                f"远程媒体下载失败：{type(error).__name__}",
                status_code=502,
            ) from error

        if response is None or not 200 <= response.status_code < 300:
            status = response.status_code if response is not None else 502
            raise DifyMediaProxyError(f"远程媒体返回 HTTP {status}", status_code=502)

        content_length = response.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > MAX_MEDIA_BYTES:
                    raise DifyMediaProxyError("远程媒体超过大小限制", status_code=502)
            except ValueError:
                # An invalid length header is harmless; the actual payload is
                # checked below before it is returned.
                pass

        payload = response.content
        if not payload or len(payload) > MAX_MEDIA_BYTES:
            raise DifyMediaProxyError("远程媒体为空或超过大小限制", status_code=502)

        declared_mime = _normalise_mime(response.headers.get("content-type"))
        sniffed_mime = _sniff_media_type(payload, kind)
        if sniffed_mime is None and kind == "audio" and payload[:4] == b"RIFF" and payload[8:12] == b"WAVE":
            repaired = _normalise_wav(payload)
            if repaired is not None:
                payload = repaired
                sniffed_mime = _sniff_media_type(payload, kind)

        if sniffed_mime is None:
            raise DifyMediaProxyError("远程媒体格式无法验证", status_code=502)
        if (
            declared_mime
            and declared_mime not in {"application/octet-stream", "binary/octet-stream"}
            and not _mime_compatible(declared_mime, sniffed_mime)
        ):
            raise DifyMediaProxyError("远程媒体类型与内容不匹配", status_code=502)
        return DifyFetchedMedia(payload=payload, content_type=sniffed_mime)

    async def run_text_to_speech(self, text: str, voice: str, user: str) -> DifyMediaResult:
        return await self._run_media(
            "audio",
            {"text": text, "voice": voice or "default"},
            user,
            settings.dify_tts_api_key,
        )

    async def run_tts(self, text: str, voice: str, user: str) -> DifyMediaResult:
        """Short alias used by callers that refer to the workflow as TTS."""
        return await self.run_text_to_speech(text, voice, user)

    async def run_text_to_image(self, prompt: str, size: str, user: str) -> DifyMediaResult:
        return await self._run_media(
            "image",
            {"prompt": prompt, "size": _normalise_image_size(size)},
            user,
            settings.dify_image_api_key,
        )

    async def run_image(self, prompt: str, size: str, user: str) -> DifyMediaResult:
        """Short alias used by callers that refer to the workflow as image."""
        return await self.run_text_to_image(prompt, size, user)
