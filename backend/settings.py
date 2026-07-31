"""Operator settings that outlive a restart — currently Telegram delivery.

Kept out of ``config.py`` on purpose: everything there is read once from the
environment and is immutable for the life of the process, whereas these are
edited from the console at runtime and must persist. They live in a single JSON
file beside the episode store.

**The bot token is a live credential.** It is stored chmod 600, never rendered
back to the browser (only a mask), and never logged. The environment is still
the better place for it — ``TELEGRAM_BOT_TOKEN`` seeds the value and, if the
console is never used to change it, the secret never touches disk at all.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

import config

logger = logging.getLogger(__name__)

_lock = threading.Lock()

#: Written next to the episode store, not inside it, so it survives pruning and
#: is never mistaken for episode content.
PATH = config.OUTPUT_DIR.parent / "settings.json"

DEFAULTS: dict[str, Any] = {
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "telegram_auto_send": False,
}

#: What the console posts back when the operator did not retype the token.
#: Anything equal to this means "keep what is already stored".
MASK_SENTINEL = "•" * 8


def _env_defaults() -> dict[str, Any]:
    raw_auto = (os.getenv("TELEGRAM_AUTO_SEND") or "").strip().lower()
    return {
        "telegram_bot_token": (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip(),
        "telegram_chat_id": (os.getenv("TELEGRAM_CHAT_ID") or "").strip(),
        "telegram_auto_send": raw_auto in ("1", "true", "yes", "on"),
    }


def load() -> dict[str, Any]:
    """Current settings: defaults ← environment ← stored file.

    The stored file wins because it represents a deliberate choice made in the
    console; the environment only seeds the initial value.
    """
    values = {**DEFAULTS, **_env_defaults()}
    try:
        with PATH.open(encoding="utf-8") as handle:
            stored = json.load(handle)
        if isinstance(stored, dict):
            for key in DEFAULTS:
                if key in stored and stored[key] is not None:
                    values[key] = stored[key]
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        logger.warning("settings file unreadable (%s); using environment defaults", exc)
    values["telegram_auto_send"] = bool(values.get("telegram_auto_send"))
    values["telegram_bot_token"] = str(values.get("telegram_bot_token") or "").strip()
    values["telegram_chat_id"] = str(values.get("telegram_chat_id") or "").strip()
    return values


def save(**changes: Any) -> dict[str, Any]:
    """Merge ``changes`` into the stored settings and persist them atomically."""
    with _lock:
        current = load()
        for key, value in changes.items():
            if key not in DEFAULTS:
                continue
            if key == "telegram_bot_token":
                # The console renders a mask, not the secret. Getting the mask
                # back means "unchanged" — writing it would destroy the token.
                if value == MASK_SENTINEL:
                    continue
                value = str(value or "").strip()
            elif key == "telegram_chat_id":
                value = str(value or "").strip()
            else:
                value = bool(value)
            current[key] = value

        PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PATH.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(current, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)  # a live bot token — not world-readable
        tmp.replace(PATH)
        logger.info(
            "settings saved (auto_send=%s, chat=%s, token=%s)",
            current["telegram_auto_send"],
            current["telegram_chat_id"] or "unset",
            "set" if current["telegram_bot_token"] else "unset",
        )
        return current


def mask_token(token: str) -> str:
    """A recognisable stand-in for the token — never the token itself.

    Telegram tokens look like ``<bot_id>:<secret>``. The bot id half is not
    secret and showing it lets you tell two bots apart at a glance.
    """
    token = (token or "").strip()
    if not token:
        return ""
    bot_id, _, secret = token.partition(":")
    if secret and bot_id.isdigit():
        return f"{bot_id}:{'•' * 6}{secret[-4:]}" if len(secret) > 4 else f"{bot_id}:{'•' * 6}"
    return "•" * 6 + token[-4:] if len(token) > 4 else "•" * 6


def public_view() -> dict[str, Any]:
    """Settings shaped for display: the token replaced by its mask."""
    values = load()
    return {
        "telegram_bot_token_mask": mask_token(values["telegram_bot_token"]),
        "telegram_bot_token_set": bool(values["telegram_bot_token"]),
        "telegram_chat_id": values["telegram_chat_id"],
        "telegram_auto_send": values["telegram_auto_send"],
        "configured": bool(values["telegram_bot_token"] and values["telegram_chat_id"]),
    }


def delivery_ready() -> tuple[bool, str]:
    """Can an episode actually be delivered right now?"""
    values = load()
    if not values["telegram_bot_token"]:
        return False, "no Telegram bot token configured"
    if not values["telegram_chat_id"]:
        return False, "no Telegram chat id configured"
    return True, ""
