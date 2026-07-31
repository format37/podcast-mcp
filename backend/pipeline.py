"""Render orchestration: script in, finished episode on disk.

Shared by both entry points — the ``render.py`` CLI and the MCP server's worker
thread — so there is exactly one implementation of the state machine, the retry
policy and the sanity checks.

Blocks render **sequentially**. That is not laziness: the account is metered,
v3 is flaky, and a failure on block 4 of 12 should have cost four blocks of
credits, not twelve. Each block's mp3 and metadata is persisted before the next
one starts (spec §5.6), so a failed job leaves behind everything it paid for.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests

import chunker
import config
import delivery
import episodes
import mixing
import script_model
import settings
import tts
from chunker import Block
from script_model import Turn

logger = logging.getLogger(__name__)

ProgressFn = Callable[[str, str, int], None]


class RenderFailed(RuntimeError):
    """A render that ran out of attempts. The message is written into job state."""


@dataclass(slots=True)
class Plan:
    """The result of validating and chunking, before a single credit is spent."""

    turns: list[Turn]
    blocks: list[Block]
    voice_a: str
    voice_b: str
    language: str

    @property
    def chars(self) -> int:
        return script_model.total_chars(self.turns)

    def estimate(self) -> dict[str, Any]:
        chars = self.chars
        cps, _, _, calibrated = config.rates_for(self.language)
        return {
            "chars": chars,
            "blocks": len(self.blocks),
            "turns": len(self.turns),
            # Audio tags are part of the text and are billed like any other
            # character. The per-character rate is measured, not documented —
            # see config.CREDITS_PER_CHAR.
            "estimated_credits": round(chars * config.CREDITS_PER_CHAR),
            "estimated_duration_s": round(chars / cps),
            "estimated_duration_human": _hms(chars / cps),
            "chars_per_second_assumed": cps,
            # Japanese runs ~5.6 c/s against English's 14.1, so a length
            # estimate is meaningless without saying which rate produced it.
            "rate_calibrated_for_language": calibrated,
            "block_plan": chunker.summarize(self.blocks),
        }


def _hms(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 60}m {seconds % 60:02d}s"


def plan(
    script: Any,
    *,
    language: str = "en",
    voice_a: str | None = None,
    voice_b: str | None = None,
) -> Plan:
    """Validate and chunk. Raises :class:`script_model.ScriptError` on a bad script."""
    turns = script_model.parse_script(script)
    blocks = chunker.chunk(turns)
    resolved_a, resolved_b = config.voices_for(language, voice_a, voice_b)
    return Plan(
        turns=turns,
        blocks=blocks,
        voice_a=resolved_a,
        voice_b=resolved_b,
        language=(language or "").strip().lower(),
    )


def _sanity(chars: int, duration: float, language: str | None) -> tuple[bool, float, str]:
    """Is a rendered block plausibly the right length? (spec §5.5)

    The window is per-language. A global one is actively harmful: Japanese
    renders at ~5.6 chars/s against English's ~14.1, so an English-derived floor
    rejects every correct Japanese block — three paid retries, then a failed job.
    """
    if duration <= 0:
        return False, 0.0, "rendered audio has zero duration"
    cps = chars / duration
    _, low, high, _ = config.rates_for(language)
    if cps > high:
        return False, cps, (
            f"rendered audio is too short: {duration:.1f}s for {chars} chars "
            f"({cps:.1f} chars/s, over the {high:.1f} ceiling for language "
            f"{language or 'unknown'!r}) — the model likely truncated the block"
        )
    if cps < low:
        return False, cps, (
            f"rendered audio is too long: {duration:.1f}s for {chars} chars "
            f"({cps:.1f} chars/s, under the {low:.1f} floor for language "
            f"{language or 'unknown'!r}) — the model likely emitted silence or looped"
        )
    return True, cps, ""


def render_block_to_file(
    block: Block,
    destination: Path,
    *,
    voice_a: str,
    voice_b: str,
    language: str | None,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Render one block, retrying transport errors *and* implausible audio.

    Returns the block's metadata. Raises :class:`RenderFailed` once
    :data:`config.MAX_BLOCK_ATTEMPTS` is exhausted, with the last real reason
    preserved — a caller should never have to guess why a block died.
    """
    inputs = block.inputs(voice_a, voice_b)
    chars = block.chars
    last_error = "unknown"
    attempts: list[dict[str, Any]] = []

    for attempt in range(1, config.MAX_BLOCK_ATTEMPTS + 1):
        started = time.monotonic()
        try:
            result = tts.render_block(
                inputs, language=language or None, session=session
            )
        except tts.TTSError as exc:
            last_error = str(exc)
            attempts.append({"attempt": attempt, "error": last_error})
            logger.warning("block %d attempt %d failed: %s", block.index, attempt, last_error)
            if not exc.retryable and exc.status is not None:
                # A rejected request will be rejected identically next time.
                raise RenderFailed(
                    f"block {block.index} was rejected by ElevenLabs and cannot be "
                    f"retried: {last_error}"
                ) from exc
            continue

        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(result.audio)

        try:
            duration = mixing.duration_s(destination)
        except mixing.MixError as exc:
            last_error = f"rendered block was not decodable audio: {exc}"
            attempts.append({"attempt": attempt, "error": last_error})
            logger.warning("block %d attempt %d: %s", block.index, attempt, last_error)
            destination.unlink(missing_ok=True)
            continue

        ok, cps, reason = _sanity(chars, duration, language)
        if not ok:
            last_error = reason
            attempts.append(
                {"attempt": attempt, "error": reason, "duration_s": round(duration, 2)}
            )
            logger.warning("block %d attempt %d rejected: %s", block.index, attempt, reason)
            destination.unlink(missing_ok=True)
            continue

        return {
            "index": block.index,
            "chars": chars,
            "turns": len(block.turns),
            "boundary": block.boundary,
            "request_id": result.request_id,
            "credits": result.credits,
            "duration_s": round(duration, 2),
            "chars_per_second": round(cps, 2),
            "attempts": attempt,
            "attempt_log": attempts,
            "api_elapsed_s": result.elapsed_s,
            "wall_elapsed_s": round(time.monotonic() - started, 2),
            "file": destination.name,
        }

    raise RenderFailed(
        f"block {block.index} failed after {config.MAX_BLOCK_ATTEMPTS} attempts. "
        f"Last failure: {last_error}"
    )


def deliver(episode_id: str) -> dict[str, Any]:
    """Send a finished episode to Telegram and record the outcome on the episode.

    Used by both the auto-send hook and the console's manual send button, so
    there is one delivery path and one shape of ``meta['delivery']``.
    """
    meta = episodes.load(episode_id)
    if meta.get("state") != "done":
        raise delivery.DeliveryError(
            f"episode is {meta.get('state')}, not done — there is nothing to send yet."
        )
    try:
        record = delivery.send_episode(meta, episodes.audio_path(episode_id))
    except delivery.DeliveryError as exc:
        episodes.update(episode_id, delivery=delivery.failure_record(str(exc)))
        raise
    episodes.update(episode_id, delivery=record)
    return record


def _auto_deliver(episode_id: str) -> None:
    """Deliver if auto-send is on. Never raises: the render already succeeded.

    A failure here is a notification problem, not a render problem — the audio
    is on disk and downloadable either way — so it is recorded on the episode
    and surfaced in the console rather than turned into a failed job.
    """
    try:
        if not settings.load().get("telegram_auto_send"):
            return
        ready, reason = settings.delivery_ready()
        if not ready:
            logger.warning("auto-send is on but %s; skipping episode %s", reason, episode_id)
            episodes.update(episode_id, delivery=delivery.skipped_record(reason))
            return
        deliver(episode_id)
    except delivery.DeliveryError as exc:
        logger.warning("auto-send failed for %s: %s", episode_id, exc)
    except Exception:  # noqa: BLE001 — a delivery bug must not undo a paid render
        logger.exception("unexpected error while auto-sending %s", episode_id)


def render(
    episode_id: str,
    prepared: Plan,
    *,
    title: str,
    on_progress: ProgressFn | None = None,
) -> dict[str, Any]:
    """Run the full pipeline for an already-created episode, updating its state.

    Any exception is recorded in the episode's ``meta.json`` as state ``failed``
    before it propagates, so a crashed worker still leaves an honest status.
    """

    def progress(state: str, message: str, block: int = 0) -> None:
        episodes.set_state(episode_id, state, message=message, block=block)
        if on_progress:
            on_progress(state, message, block)

    directory = episodes.episode_dir(episode_id)
    block_dir = episodes.blocks_dir(episode_id)
    total = len(prepared.blocks)

    try:
        progress("validated", f"{prepared.chars} chars in {total} block(s)")

        rendered: list[Path] = []
        credits = 0
        with requests.Session() as session:
            for block in prepared.blocks:
                progress(
                    "rendering",
                    f"block {block.index + 1}/{total} ({block.chars} chars)",
                    block.index + 1,
                )
                destination = block_dir / f"{block.index:03d}.mp3"
                info = render_block_to_file(
                    block,
                    destination,
                    voice_a=prepared.voice_a,
                    voice_b=prepared.voice_b,
                    language=prepared.language,
                    session=session,
                )
                episodes.record_block(episode_id, block.index, info)
                credits += info.get("credits") or 0
                rendered.append(destination)

        progress("mixing", f"joining {total} block(s), normalising to {config.LOUDNORM_I} LUFS", total)
        destination = episodes.audio_path(episode_id)
        stats = mixing.mix(rendered, destination, work_dir=directory)
        mixing.tag(
            destination,
            title=title,
            artist=config.SHOW_TITLE,
            album=config.SHOW_TITLE,
            cover=Path(config.SHOW_IMAGE) if config.SHOW_IMAGE else None,
        )
        # Tagging rewrites the file, so re-read the size it actually shipped at.
        stats["size_bytes"] = destination.stat().st_size

        episode = {
            "filename": destination.name,
            "duration_s": stats["duration_s"],
            "duration_human": _hms(stats["duration_s"]),
            "size_bytes": stats["size_bytes"],
            "size_mb": round(stats["size_bytes"] / 1_048_576, 2),
            "path": str(destination),
            "credits_reported": credits or None,
            "chars_per_second": (
                round(prepared.chars / stats["duration_s"], 2) if stats["duration_s"] else None
            ),
        }
        episodes.update(episode_id, episode=episode)
        episodes.cleanup_blocks(episode_id)
        progress("done", f"{episode['duration_human']}, {episode['size_mb']} MB", total)
        # Rewritten only after the state reaches "done", so request.yaml's
        # result section records the finished episode rather than the last
        # step of the pipeline that produced it.
        episodes.write_sidecars(episode_id)
        _auto_deliver(episode_id)
        logger.info(
            "episode %s rendered: %s, %s MB, %s chars/s",
            episode_id, episode["duration_human"], episode["size_mb"],
            episode["chars_per_second"],
        )
        return episodes.load(episode_id)

    except Exception as exc:  # noqa: BLE001 — the failure must reach job state
        logger.exception("episode %s failed", episode_id)
        episodes.set_state(episode_id, "failed", message="render failed", error=str(exc))
        raise
