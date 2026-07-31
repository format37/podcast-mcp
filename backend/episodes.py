"""Episode store — one directory per episode, and the job state lives in it.

There is no separate job table. A job *is* an episode being built, so
``job_id == episode_id`` and the whole state machine is one ``meta.json``
written atomically after every step. That gives spec §2's "persist job state to
disk so a server restart doesn't lose history" for free, and makes
``podcast_status`` a file read rather than a lookup into process memory that a
restart would have emptied.

Layout::

    <OUTPUT_DIR>/<episode_id>/
        meta.json       state machine + per-block render metadata
        script.json     exactly what the agent submitted
        episode.mp3     the finished mix (only once state == done)
        blocks/000.mp3  per-block renders, kept for seam debugging
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import brief
import config

logger = logging.getLogger(__name__)

#: Every state a job can be in. ``podcast_status`` never reports anything else.
STATES = (
    "queued",
    "validated",
    "rendering",
    "mixing",
    "done",
    "failed",
    "interrupted",
)
_TERMINAL = {"done", "failed", "interrupted"}

_ID_RE = re.compile(r"^\d{8}-[0-9a-f]{12}$")
_write_lock = threading.Lock()


class EpisodeError(Exception):
    """Unknown episode id, or a store operation that could not be completed."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def root() -> Path:
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return config.OUTPUT_DIR


def new_id() -> str:
    """Date-prefixed so the store sorts chronologically in a plain ``ls``."""
    return f"{datetime.now(timezone.utc):%Y%m%d}-{uuid.uuid4().hex[:12]}"


def _validate_id(episode_id: Any) -> str:
    """Reject anything that is not one of our ids — these become path segments."""
    value = str(episode_id or "").strip()
    if not _ID_RE.match(value):
        raise EpisodeError(
            f"{episode_id!r} is not a valid episode/job id. Ids look like "
            "'20260731-a1b2c3d4e5f6'; list them with podcast_list."
        )
    return value


def episode_dir(episode_id: str) -> Path:
    return root() / _validate_id(episode_id)


def blocks_dir(episode_id: str) -> Path:
    return episode_dir(episode_id) / "blocks"


def audio_path(episode_id: str) -> Path:
    return episode_dir(episode_id) / "episode.mp3"


def script_path(episode_id: str) -> Path:
    return episode_dir(episode_id) / "script.json"


def meta_path(episode_id: str) -> Path:
    return episode_dir(episode_id) / "meta.json"


def request_path(episode_id: str) -> Path:
    return episode_dir(episode_id) / "request.yaml"


def text_path(episode_id: str) -> Path:
    return episode_dir(episode_id) / "script.txt"


def _write_json(path: Path, payload: Any) -> None:
    """Atomic replace: a crash mid-write must not leave unreadable job state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)


def write_sidecars(episode_id: str) -> None:
    """(Re)write ``request.yaml`` and ``script.txt`` from current meta + script.

    Called at creation and again when a render finishes, so the yaml's ``result``
    section reflects the episode that actually shipped. Never fatal: these are
    console conveniences, and a render that succeeded must not be reported as
    failed because a sidecar could not be written.
    """
    try:
        meta = load(episode_id)
        script = load_script(episode_id)
        record = brief.build(meta, meta.get("brief"))
        request_path(episode_id).write_text(brief.to_yaml(record), encoding="utf-8")
        text_path(episode_id).write_text(
            brief.script_to_text(script, title=meta.get("title", ""), meta=meta),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not write sidecars for %s: %s", episode_id, exc)


def create(
    *,
    title: str,
    description: str,
    language: str,
    voice_a: str,
    voice_b: str,
    chars: int,
    blocks: list[dict[str, Any]],
    script: list[dict[str, Any]],
    brief_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Register a new job/episode in state ``queued`` and persist its script."""
    episode_id = new_id()
    meta = {
        "id": episode_id,
        "title": title,
        "description": description,
        "language": language,
        "state": "queued",
        "created": _now(),
        "updated": _now(),
        "model_id": config.MODEL_ID,
        "voice_a": voice_a,
        "voice_b": voice_b,
        "chars": chars,
        "block_count": len(blocks),
        "blocks": blocks,
        # The agent's own account of what this episode is for. Kept in meta so
        # request.yaml can be rebuilt whenever the episode's facts change.
        "brief": brief.clean_brief(brief_data),
        "progress": {"block": 0, "blocks": len(blocks), "message": "queued"},
        "error": None,
        "episode": None,
    }
    blocks_dir(episode_id).mkdir(parents=True, exist_ok=True)
    _write_json(script_path(episode_id), script)
    _write_json(meta_path(episode_id), meta)
    write_sidecars(episode_id)
    logger.info("episode %s created: %r (%d chars, %d blocks)", episode_id, title, chars, len(blocks))
    return meta


def load(episode_id: str) -> dict[str, Any]:
    path = meta_path(episode_id)
    if not path.is_file():
        raise EpisodeError(
            f"No episode or job with id {episode_id!r}. It may have been deleted, or "
            "the id may be from a different OUTPUT_DIR. Use podcast_list to see what exists."
        )
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except ValueError as exc:
        raise EpisodeError(f"episode {episode_id} has unreadable meta.json: {exc}") from exc


def update(episode_id: str, **fields: Any) -> dict[str, Any]:
    """Read-modify-write ``meta.json`` under a lock; returns the new meta."""
    with _write_lock:
        meta = load(episode_id)
        meta.update(fields)
        meta["updated"] = _now()
        _write_json(meta_path(episode_id), meta)
        return meta


def set_state(
    episode_id: str,
    state: str,
    *,
    message: str = "",
    block: int | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    if state not in STATES:
        raise EpisodeError(f"unknown state {state!r}; expected one of {', '.join(STATES)}")
    with _write_lock:
        meta = load(episode_id)
        meta["state"] = state
        progress = dict(meta.get("progress") or {})
        if block is not None:
            progress["block"] = block
        progress["message"] = message or state
        meta["progress"] = progress
        if error is not None:
            meta["error"] = error
        meta["updated"] = _now()
        _write_json(meta_path(episode_id), meta)
    logger.info("episode %s -> %s%s", episode_id, state, f" ({message})" if message else "")
    return meta


def record_block(episode_id: str, index: int, info: dict[str, Any]) -> None:
    """Merge one block's render metadata into ``meta.blocks[index]``."""
    with _write_lock:
        meta = load(episode_id)
        blocks = list(meta.get("blocks") or [])
        if 0 <= index < len(blocks):
            merged = dict(blocks[index])
            merged.update(info)
            blocks[index] = merged
            meta["blocks"] = blocks
        meta["updated"] = _now()
        _write_json(meta_path(episode_id), meta)


def load_script(episode_id: str) -> list[dict[str, Any]]:
    path = script_path(episode_id)
    if not path.is_file():
        raise EpisodeError(f"episode {episode_id} has no stored script.")
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def listing(limit: int = 20, *, include_unfinished: bool = True) -> list[dict[str, Any]]:
    """Newest-first summaries. Unreadable directories are skipped, never fatal."""
    out: list[dict[str, Any]] = []
    for path in sorted(root().glob("*/meta.json"), reverse=True):
        try:
            with path.open(encoding="utf-8") as handle:
                meta = json.load(handle)
        except (OSError, ValueError):
            continue
        if not include_unfinished and meta.get("state") != "done":
            continue
        episode = meta.get("episode") or {}
        brief_data = meta.get("brief") or {}
        out.append(
            {
                "id": meta.get("id"),
                "title": meta.get("title"),
                "state": meta.get("state"),
                "created": meta.get("created"),
                "language": meta.get("language"),
                "chars": meta.get("chars"),
                "duration_s": episode.get("duration_s"),
                "size_bytes": episode.get("size_bytes"),
                "error": meta.get("error"),
                "description": meta.get("description"),
                # Carried in the listing so an agent planning a new episode can
                # see what ground is already covered without opening each one.
                "topic": brief_data.get("topic"),
                "angle": brief_data.get("angle"),
            }
        )
        if len(out) >= max(1, limit):
            break
    return out


def delete(episode_id: str) -> dict[str, Any]:
    directory = episode_dir(episode_id)
    if not directory.is_dir():
        raise EpisodeError(f"No episode with id {episode_id!r} to delete.")
    meta = {}
    try:
        meta = load(episode_id)
    except EpisodeError:
        pass
    if meta.get("state") in ("queued", "rendering", "mixing"):
        raise EpisodeError(
            f"episode {episode_id} is still {meta.get('state')} — wait for it to finish "
            "or fail before deleting, so the worker does not write into a deleted directory."
        )
    shutil.rmtree(directory)
    logger.info("episode %s deleted", episode_id)
    return {"deleted": episode_id, "title": meta.get("title")}


def cleanup_blocks(episode_id: str) -> None:
    """Drop per-block mp3s once the mix succeeded (only when KEEP_BLOCKS is off)."""
    if config.KEEP_BLOCKS:
        return
    shutil.rmtree(blocks_dir(episode_id), ignore_errors=True)


def reap_interrupted() -> int:
    """Mark jobs that were mid-flight when the process died.

    Called at startup. Without it a killed render leaves a job stuck at
    "rendering" forever, and ``podcast_status`` would keep reporting progress
    for a worker that no longer exists — exactly the lie spec §8 forbids.
    """
    count = 0
    for path in root().glob("*/meta.json"):
        try:
            with path.open(encoding="utf-8") as handle:
                meta = json.load(handle)
        except (OSError, ValueError):
            continue
        if meta.get("state") in ("queued", "rendering", "mixing"):
            episode_id = meta.get("id")
            if not episode_id:
                continue
            try:
                set_state(
                    episode_id,
                    "interrupted",
                    message="server restarted while this job was running",
                    error=(
                        "The server restarted mid-render, so this job was abandoned. "
                        "Its finished blocks are still on disk; re-submit the script "
                        "with podcast_generate to render it again."
                    ),
                )
                count += 1
            except EpisodeError:
                continue
    if count:
        logger.warning("marked %d interrupted job(s) after restart", count)
    return count


def prune(max_age_days: int) -> int:
    """Delete finished episodes older than ``max_age_days`` (0 disables pruning)."""
    if max_age_days <= 0:
        return 0
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    for path in list(root().glob("*/meta.json")):
        try:
            if path.stat().st_mtime >= cutoff:
                continue
            with path.open(encoding="utf-8") as handle:
                meta = json.load(handle)
        except (OSError, ValueError):
            continue
        if meta.get("state") in _TERMINAL:
            shutil.rmtree(path.parent, ignore_errors=True)
            removed += 1
    return removed
