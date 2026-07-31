"""ElevenLabs text-to-dialogue client.

One function that matters: :func:`render_block`, which turns a block's inputs
into mp3 bytes. Everything around it is failure handling, because ``eleven_v3``
is an alpha model on a metered account — a request that fails silently is worse
than one that errors, and both are worse than one that costs credits twice.

Notably absent: any conditioning on the previous block. ``/v1/text-to-dialogue``
has no ``previous_request_ids`` field, and request stitching is documented as
unavailable for ``eleven_v3`` (spec §10 Q1 — resolved: no).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import requests

import config

logger = logging.getLogger(__name__)


class TTSError(RuntimeError):
    """A render request that could not be completed."""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


@dataclass(slots=True)
class RenderResult:
    audio: bytes
    request_id: str | None
    status: int
    #: Credit cost reported by the response headers, when the API sends it.
    credits: int | None
    elapsed_s: float


def _headers() -> dict[str, str]:
    if not config.ELEVENLABS_API_KEY:
        raise TTSError(
            "ELEVENLABS_API_KEY is not set — the server cannot render audio. "
            "Set it in the .env mounted at /app/.env and restart."
        )
    return {
        "xi-api-key": config.ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }


def _extract_credits(headers: Any) -> int | None:
    """Pull the credit cost out of whichever header the API used for it."""
    for key in (
        "character-cost",
        "x-character-cost",
        "current-concurrent-requests",  # not a cost; skipped below
    ):
        if key == "current-concurrent-requests":
            continue
        value = headers.get(key)
        if value:
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
    return None


def _request_id(headers: Any) -> str | None:
    for key in ("request-id", "x-request-id", "request_id"):
        value = headers.get(key)
        if value:
            return str(value)
    return None


def _error_detail(response: requests.Response) -> str:
    """Best-effort human-readable reason from an error response body."""
    try:
        payload = response.json()
    except ValueError:
        text = (response.text or "").strip()
        return text[:400] or f"HTTP {response.status_code}"
    detail = payload.get("detail", payload) if isinstance(payload, dict) else payload
    if isinstance(detail, dict):
        message = detail.get("message") or detail.get("msg")
        status = detail.get("status")
        if message:
            return f"{status}: {message}" if status else str(message)
    return str(detail)[:400]


def render_block(
    inputs: list[dict[str, str]],
    *,
    language: str | None = None,
    seed: int | None = None,
    session: requests.Session | None = None,
) -> RenderResult:
    """POST one block to ``/v1/text-to-dialogue``; retry only transient failures.

    Rate limits and 5xx are retried with exponential backoff inside a total
    :data:`config.RATE_LIMIT_MAX_WAIT_S` budget (spec §8). A 4xx that is not 429
    is a bad request — retrying it just burns time, so it raises immediately.
    """
    url = f"{config.API_BASE}/v1/text-to-dialogue"
    params = {"output_format": config.OUTPUT_FORMAT}
    body: dict[str, Any] = {
        "inputs": inputs,
        "model_id": config.MODEL_ID,
        "settings": {"stability": config.STABILITY},
    }
    # v3 auto-detects language; pin it only when the caller was explicit, since
    # a wrong pin is worse than none.
    if language:
        body["language_code"] = language
    if seed is not None:
        body["seed"] = seed

    http = session or requests
    waited = 0.0
    backoff = 2.0
    attempt = 0

    while True:
        attempt += 1
        started = time.monotonic()
        try:
            response = http.post(
                url,
                params=params,
                json=body,
                headers=_headers(),
                timeout=config.REQUEST_TIMEOUT_S,
            )
        except requests.Timeout as exc:
            raise TTSError(
                f"text-to-dialogue timed out after {config.REQUEST_TIMEOUT_S:.0f}s: {exc}",
                retryable=True,
            ) from exc
        except requests.RequestException as exc:
            raise TTSError(f"text-to-dialogue request failed: {exc}", retryable=True) from exc

        elapsed = time.monotonic() - started

        if response.status_code == 200:
            audio = response.content
            if not audio:
                raise TTSError("text-to-dialogue returned 200 with an empty body", retryable=True)
            return RenderResult(
                audio=audio,
                request_id=_request_id(response.headers),
                status=response.status_code,
                credits=_extract_credits(response.headers),
                elapsed_s=round(elapsed, 2),
            )

        detail = _error_detail(response)
        transient = response.status_code == 429 or response.status_code >= 500

        if not transient:
            raise TTSError(
                f"text-to-dialogue rejected the request (HTTP {response.status_code}): {detail}",
                status=response.status_code,
            )

        # Honour Retry-After when the API sends one; otherwise back off.
        retry_after = response.headers.get("retry-after")
        try:
            delay = float(retry_after) if retry_after else backoff
        except (TypeError, ValueError):
            delay = backoff
        delay = min(delay, max(0.0, config.RATE_LIMIT_MAX_WAIT_S - waited))

        if delay <= 0:
            raise TTSError(
                f"text-to-dialogue kept returning HTTP {response.status_code} for "
                f"{config.RATE_LIMIT_MAX_WAIT_S:.0f}s ({detail}); giving up",
                status=response.status_code,
                retryable=True,
            )

        logger.warning(
            "text-to-dialogue HTTP %s (%s); retrying in %.0fs (attempt %d, %.0fs of %.0fs budget used)",
            response.status_code, detail, delay, attempt, waited, config.RATE_LIMIT_MAX_WAIT_S,
        )
        time.sleep(delay)
        waited += delay
        backoff = min(backoff * 2, 60.0)


def subscription() -> dict[str, Any]:
    """Character quota snapshot, for the running credit counter in ``podcast_list``."""
    response = requests.get(
        f"{config.API_BASE}/v1/user/subscription",
        headers={"xi-api-key": config.ELEVENLABS_API_KEY},
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    used = data.get("character_count")
    limit = data.get("character_limit")
    return {
        "tier": data.get("tier"),
        "characters_used": used,
        "characters_limit": limit,
        "characters_remaining": (limit - used) if isinstance(used, int) and isinstance(limit, int) else None,
        "resets_unix": data.get("next_character_count_reset_unix"),
    }


def list_voices(page_size: int = 100) -> list[dict[str, Any]]:
    """Voices available on the account, trimmed to what a host-casting decision needs."""
    response = requests.get(
        f"{config.API_BASE}/v2/voices",
        params={"page_size": max(1, min(page_size, 100))},
        headers={"xi-api-key": config.ELEVENLABS_API_KEY},
        timeout=30,
    )
    response.raise_for_status()
    out = []
    for voice in response.json().get("voices", []):
        labels = voice.get("labels") or {}
        out.append(
            {
                "voice_id": voice.get("voice_id"),
                "name": voice.get("name"),
                "category": voice.get("category"),
                "gender": labels.get("gender"),
                "accent": labels.get("accent"),
                "description": labels.get("description"),
                "use_case": labels.get("use_case"),
            }
        )
    return out
