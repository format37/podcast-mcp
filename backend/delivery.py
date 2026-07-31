"""Telegram delivery — upload a finished episode with ``sendAudio``.

``sendAudio`` rather than ``sendDocument`` or ``sendVoice``: it renders an
inline player with seek and 1.5x/2x speed, shows title/performer metadata, and
takes cover art. That is the difference between an episode you listen to in the
chat and a file you have to download first.

The file is **uploaded** rather than passed as a URL. Telegram's send-by-URL
path caps at 20 MB and requires the episode route to be publicly reachable;
uploading works up to 50 MB and needs no public origin at all, so a
localhost-only deployment delivers exactly like the VPS one.

Delivery never fails a render. An episode that exists but did not reach Telegram
is a notification problem; the audio is on disk either way.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

import config
import settings as settings_mod

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"

#: Bot API multipart upload ceiling.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
#: Telegram truncates captions past this; we do it ourselves so the cut is clean.
MAX_CAPTION = 1024
#: Thumbnails over 200 KB are rejected.
MAX_THUMB_BYTES = 200 * 1024


class DeliveryError(RuntimeError):
    """Delivery could not be completed. The message is safe to show the operator."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _api(token: str, method: str, *, data=None, files=None, timeout=180.0) -> dict[str, Any]:
    """Call one Bot API method. Raises :class:`DeliveryError` with Telegram's own reason."""
    if not token:
        raise DeliveryError("no Telegram bot token configured")
    try:
        response = requests.post(
            f"{API_BASE}/bot{token}/{method}", data=data, files=files, timeout=timeout
        )
    except requests.Timeout as exc:
        raise DeliveryError(f"Telegram timed out after {timeout:.0f}s") from exc
    except requests.RequestException as exc:
        raise DeliveryError(f"could not reach Telegram: {exc}") from exc

    try:
        payload = response.json()
    except ValueError:
        raise DeliveryError(f"Telegram returned HTTP {response.status_code} with a non-JSON body") from None

    if not payload.get("ok"):
        # description is Telegram's own wording ("chat not found", "Unauthorized")
        # and is far more useful than anything we could infer from the code.
        detail = payload.get("description") or f"HTTP {response.status_code}"
        raise DeliveryError(f"Telegram refused the request: {detail}")
    return payload.get("result") or {}


def check(token: str = "", chat_id: str = "") -> dict[str, Any]:
    """Verify the bot token and that the chat is reachable.

    Sends a real message: ``getMe`` only proves the token is valid, which is the
    half that rarely goes wrong. A wrong chat id, or a bot that was never
    started by the user, only surfaces on an actual send.
    """
    values = settings_mod.load()
    token = (token or values["telegram_bot_token"]).strip()
    chat_id = (chat_id or values["telegram_chat_id"]).strip()

    me = _api(token, "getMe", timeout=30)
    username = me.get("username") or me.get("first_name") or "unknown"
    if not chat_id:
        return {"ok": True, "bot": username, "chat": None,
                "detail": f"Token valid (@{username}). No chat id set, so nothing was sent."}

    sent = _api(
        token, "sendMessage",
        data={
            "chat_id": chat_id,
            "text": f"✅ {config.SHOW_TITLE} — podcast delivery is configured.",
            "disable_notification": "true",
        },
        timeout=30,
    )
    chat = sent.get("chat") or {}
    name = chat.get("title") or chat.get("username") or chat.get("first_name") or chat_id
    return {"ok": True, "bot": username, "chat": name,
            "detail": f"Sent a test message as @{username} to {name}."}


def _caption(meta: dict[str, Any]) -> str:
    parts = [p for p in (meta.get("title"), meta.get("description")) if p]
    text = "\n\n".join(str(p).strip() for p in parts)
    if len(text) > MAX_CAPTION:
        text = text[: MAX_CAPTION - 1].rstrip() + "…"
    return text


def send_episode(meta: dict[str, Any], audio_path: Path) -> dict[str, Any]:
    """Upload one finished episode. Returns a record for ``meta['delivery']``."""
    ready, reason = settings_mod.delivery_ready()
    if not ready:
        raise DeliveryError(reason)
    values = settings_mod.load()

    if not audio_path.is_file():
        raise DeliveryError(f"episode audio is missing at {audio_path.name}")
    size = audio_path.stat().st_size
    if size > MAX_UPLOAD_BYTES:
        raise DeliveryError(
            f"episode is {size / 1_048_576:.1f} MB, over Telegram's "
            f"{MAX_UPLOAD_BYTES // 1_048_576} MB upload limit"
        )

    episode = meta.get("episode") or {}
    data = {
        "chat_id": values["telegram_chat_id"],
        "caption": _caption(meta),
        "title": str(meta.get("title") or "Episode")[:64],
        "performer": str(config.SHOW_TITLE)[:64],
    }
    duration = episode.get("duration_s")
    if duration:
        data["duration"] = str(int(round(float(duration))))

    files: dict[str, Any] = {}
    handles = []
    try:
        audio = audio_path.open("rb")
        handles.append(audio)
        files["audio"] = (f"{meta.get('id', 'episode')}.mp3", audio, "audio/mpeg")

        cover = Path(config.SHOW_IMAGE) if config.SHOW_IMAGE else None
        if cover and cover.is_file() and cover.stat().st_size <= MAX_THUMB_BYTES:
            thumb = cover.open("rb")
            handles.append(thumb)
            files["thumbnail"] = (cover.name, thumb, "image/jpeg")
        elif cover and cover.is_file():
            logger.info("cover %s is over %d KB; sending without it",
                        cover.name, MAX_THUMB_BYTES // 1024)

        result = _api(values["telegram_bot_token"], "sendAudio", data=data, files=files)
    finally:
        for handle in handles:
            handle.close()

    chat = result.get("chat") or {}
    record = {
        "state": "sent",
        "at": _now(),
        "message_id": result.get("message_id"),
        "chat": chat.get("title") or chat.get("username") or str(values["telegram_chat_id"]),
        "error": None,
    }
    logger.info("episode %s delivered to Telegram (message %s)",
                meta.get("id"), record["message_id"])
    return record


def failure_record(error: str) -> dict[str, Any]:
    return {"state": "failed", "at": _now(), "message_id": None, "chat": None, "error": error}


def skipped_record(reason: str) -> dict[str, Any]:
    """Auto-send was on but could not run — distinct from 'off', which records nothing."""
    return {"state": "skipped", "at": _now(), "message_id": None, "chat": None, "error": reason}
