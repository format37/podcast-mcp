"""podcast MCP — render a two-host dialogue script into a finished episode.

The division of labour is the point (spec §1): the *agent* researches the topic
and writes the script; this server does not think. It validates, chunks,
renders, mixes and stores. No LLM calls happen inside it, which is what keeps
the renderer swappable behind a stable tool surface.

Shape follows the kindle MCP next door: FastMCP over streamable HTTP, the same
token-in-the-path middleware (a claude.ai custom connector cannot send an
Authorization header), the same Starlette assembly. Long renders return a
``job_id`` immediately and are polled with ``podcast_status`` — a 25-minute
episode takes ~25 minutes of wall clock, far past any MCP round trip.
"""

from __future__ import annotations

import contextlib
import datetime
import logging
import os
import queue
import re
import threading
from pathlib import Path
from typing import Any

import uvicorn
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Mount, Route

# In the container .env is mounted at /app/.env and the CWD is /app; running
# from a checkout the CWD is usually backend/ while .env sits at the repo root.
# Try all three rather than silently starting with no API key.
for _candidate in (
    Path(".env"),
    Path(__file__).resolve().parent / ".env",
    Path(__file__).resolve().parent.parent / ".env",
):
    if _candidate.is_file():
        load_dotenv(_candidate)
        break

import brief as brief_mod  # noqa: E402
import config  # noqa: E402  — must import after load_dotenv
import console  # noqa: E402
import delivery  # noqa: E402
import episodes  # noqa: E402
import settings  # noqa: E402
import mixing  # noqa: E402
import pipeline  # noqa: E402
import script_model  # noqa: E402
import tts  # noqa: E402
from episodes import EpisodeError  # noqa: E402
from script_model import ScriptError  # noqa: E402

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger(__name__)

MCP_NAME = os.getenv("MCP_NAME", "podcast")
_safe_name = re.sub(r"[^a-z0-9_-]", "-", MCP_NAME.lower()).strip("-") or "service"
BASE_PATH = f"/{_safe_name}"
STREAM_PATH = f"{BASE_PATH}/"
EPISODES_ROUTE = f"{BASE_PATH}/episodes"
CONSOLE_ROUTE = f"{BASE_PATH}/console"

PORT = int(os.getenv("PORT", "8021"))
PUBLIC_BASE_URL = (os.getenv("MCP_PUBLIC_BASE_URL") or "").rstrip("/") or None
#: Secret path segment for episode downloads (spec §7). Unlike the MCP token
#: this one ends up in a URL handed to Telegram, so it is kept separate.
DOWNLOAD_TOKEN = (os.getenv("DOWNLOAD_TOKEN") or "").strip()

_TOKENS = {t.strip() for t in os.getenv("MCP_TOKENS", "").split(",") if t.strip()}

_allowed_hosts = ["localhost", "127.0.0.1", "0.0.0.0", _safe_name, f"mcp-{_safe_name}"]
_allowed_hosts += [f"{h}:*" for h in list(_allowed_hosts)]
_allowed_origins = ["http://localhost", "http://127.0.0.1"]
for _h in [x.strip() for x in os.getenv("MCP_ALLOWED_HOSTS", "").split(",") if x.strip()]:
    _allowed_hosts.append(_h)
    _allowed_hosts.append(f"{_h}:*")
    _allowed_origins.append(f"https://{_h}")

mcp = FastMCP(
    _safe_name,
    streamable_http_path=STREAM_PATH,
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_allowed_hosts,
        allowed_origins=_allowed_origins,
    ),
)


class _StreamErrorFilter(logging.Filter):
    def filter(self, record):
        return "ClosedResourceError" not in str(record.getMessage())


logging.getLogger("mcp.server.streamable_http").addFilter(_StreamErrorFilter())


class _TokenRedactingFilter(logging.Filter):
    """Tokens ride in the URL path, so without this they land in access logs."""

    def __init__(self, tokens):
        super().__init__()
        self._tokens = sorted((t for t in tokens if t), key=len, reverse=True)

    def filter(self, record):
        if self._tokens:
            try:
                message = record.getMessage()
            except Exception:  # noqa: BLE001 — never break logging over this
                return True
            redacted = message
            for token in self._tokens:
                redacted = redacted.replace(token, "***")
            if redacted != message:
                record.msg = redacted
                record.args = ()
        return True


# Attached to the root logger's HANDLERS, not the logger itself. A filter on a
# logger only sees records logged directly to it — records propagated up from
# uvicorn.access (which is exactly where request URLs, and therefore the path
# tokens, appear) bypass it entirely and would be written verbatim.
_redactor = _TokenRedactingFilter(_TOKENS | {DOWNLOAD_TOKEN})
for _handler in logging.getLogger().handlers:
    _handler.addFilter(_redactor)


# --------------------------------------------------------------------------
# Render worker
# --------------------------------------------------------------------------
#
# One thread, one queue, strictly FIFO. Not a limitation to work around: the
# ElevenLabs account is metered and single-user, and running two episodes at
# once would only trade concurrency for rate-limit retries. Job state lives in
# each episode's meta.json (see episodes.py), so a restart loses the queue but
# never the history — pending jobs are reaped into "interrupted" at startup.

_work: "queue.Queue[tuple[str, pipeline.Plan, str]]" = queue.Queue()
_worker: threading.Thread | None = None


def _worker_loop() -> None:
    while True:
        episode_id, prepared, title = _work.get()
        try:
            pipeline.render(episode_id, prepared, title=title)
        except Exception:  # noqa: BLE001 — pipeline.render already recorded it
            logger.warning("job %s ended in failure", episode_id)
        finally:
            _work.task_done()


def _ensure_worker() -> None:
    global _worker
    if _worker is None or not _worker.is_alive():
        _worker = threading.Thread(target=_worker_loop, name="render-worker", daemon=True)
        _worker.start()
        logger.info("render worker started")


def _queue_depth() -> int:
    return _work.qsize()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _episode_url(episode_id: str, filename: str = "episode.mp3") -> str | None:
    """Public download URL, or None when no public origin is configured.

    Locally there is no origin to hand out and none is needed — the telegram
    skill reads the file straight off disk, so ``path`` is the useful field.
    """
    if not PUBLIC_BASE_URL:
        return None
    prefix = f"{PUBLIC_BASE_URL}{EPISODES_ROUTE}"
    if DOWNLOAD_TOKEN:
        prefix = f"{PUBLIC_BASE_URL}{BASE_PATH}/{DOWNLOAD_TOKEN}/episodes"
    return f"{prefix}/{episode_id}/{filename}"


def _episode_view(meta: dict[str, Any]) -> dict[str, Any]:
    """Shape one episode for a tool reply, with delivery details attached."""
    episode_id = meta.get("id")
    view: dict[str, Any] = {
        "episode_id": episode_id,
        "title": meta.get("title"),
        "description": meta.get("description"),
        "language": meta.get("language"),
        "created": meta.get("created"),
        "state": meta.get("state"),
        "chars": meta.get("chars"),
        "blocks": meta.get("block_count"),
    }
    episode = meta.get("episode") or {}
    if episode:
        size_mb = episode.get("size_mb")
        view.update(
            {
                "duration_s": episode.get("duration_s"),
                "duration": episode.get("duration_human"),
                "size_mb": size_mb,
                # Translated to the host side of the bind mount: the agent runs
                # the telegram skill outside this container.
                "path": config.host_path(episode.get("path") or ""),
                "url": _episode_url(episode_id),
                "script_url": _episode_url(episode_id, "script.json"),
                # Telegram fetches a URL itself only up to 20 MB; past that the
                # skill must upload the bytes (spec §6).
                "telegram_url_delivery_ok": (size_mb or 0) <= 20,
            }
        )
    return view


def _load_record(episode_id: str, meta: dict[str, Any]) -> dict[str, Any]:
    """The episode's request record, preferring the file on disk.

    Reading request.yaml back (rather than always rebuilding from meta) means
    the console shows exactly what the downloadable file contains — including
    for episodes whose config has since changed underneath them.
    """
    path = episodes.request_path(episode_id)
    if path.is_file():
        try:
            record = brief_mod.parse_yaml(path.read_text(encoding="utf-8"))
            if record:
                return record
        except OSError:
            pass
    return brief_mod.build(meta, meta.get("brief"))


def _delivery_hint(view: dict[str, Any]) -> str:
    """Tell the agent exactly how to ship it — delivery is agent-side by design."""
    path = view.get("path")
    title = (view.get("title") or "Episode").replace('"', "'")
    return (
        "Delivery is agent-side. Send it with the telegram skill:\n"
        f'  $PY $TG audio "{path}" --title "{title}" --performer "{config.SHOW_TITLE}"'
        + (f' --thumb "{config.SHOW_IMAGE}"' if config.SHOW_IMAGE else "")
        + "\nAdd --caption with the show notes."
    )


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------


@mcp.tool()
def podcast_generate(
    title: str,
    script: list[dict],
    language: str = "en",
    description: str = "",
    voice_a: str | None = None,
    voice_b: str | None = None,
    brief: dict | None = None,
    dry_run: bool = False,
) -> dict:
    """Render a two-host dialogue script into a podcast episode.

    Returns a ``job_id`` immediately and renders in the background — poll with
    ``podcast_status``. Rendering runs at roughly 1x realtime, so a 20-minute
    episode takes about 20 minutes.

    ``script`` is a JSON array of turns, and it is the whole contract:

        [{"speaker": "HOST_A", "text": "[excited] Okay, so today we're doing ROC curves."},
         {"speaker": "HOST_B", "text": "[laughs] The curve everyone plots and nobody reads.",
          "block_break_after": true}]

    Rules, all enforced — a violation comes back as an error naming the turn and
    the fix, before any credits are spent:

    * ``speaker`` is exactly ``HOST_A`` or ``HOST_B``.
    * ``text`` is 1–500 characters, with inline audio tags in square brackets
      (``[laughs]``, ``[thoughtful]``, ``[whispers]``; stacking is allowed).
    * The whole script is at most 30,000 characters.
    * ``block_break_after: true`` marks a preferred chunk boundary. Set it at
      subtopic transitions — the script is cut into ~1,800-character render
      blocks, and every cut is an audible seam that cannot be conditioned away,
      so a seam placed on a topic change is a seam nobody hears.

    ``brief`` records *why* this episode exists, and is worth filling in every
    time: it is written into the episode's ``request.yaml`` alongside the exact
    generation config, shown in the console, and handed back to whichever agent
    plans the next episode — so episode twelve can build on episode three
    instead of repeating it.

        {"topic": "ROC curves and AUC",
         "prompt": "<the user's request, verbatim>",
         "angle": "reading the curve as a menu of thresholds, not a score",
         "sources": ["https://…"],
         "notes": "listener already knows precision/recall — don't re-explain"}

    Set ``dry_run`` to validate, chunk and cost a script without calling the API.
    """
    try:
        prepared = pipeline.plan(script, language=language, voice_a=voice_a, voice_b=voice_b)
    except ScriptError as exc:
        raise ValueError(exc.as_text()) from None

    clean_title = (title or "").strip()
    if not clean_title:
        raise ValueError("title is required — it becomes the episode's ID3 title in Telegram.")

    estimate = prepared.estimate()

    if dry_run:
        return {
            "dry_run": True,
            "title": clean_title,
            "language": prepared.language,
            "voice_a": prepared.voice_a,
            "voice_b": prepared.voice_b,
            **estimate,
            "note": (
                "Nothing was rendered and no credits were spent. "
                f"Estimates assume {config.ESTIMATE_CPS} chars/second (measured on English) "
                f"and {config.CREDITS_PER_CHAR} credits/char."
            ),
        }

    if not config.ELEVENLABS_API_KEY:
        raise ValueError(
            "ELEVENLABS_API_KEY is not configured on the server, so nothing can be "
            "rendered. dry_run=true still works for validating and costing a script."
        )
    if not mixing.available():
        raise ValueError("ffmpeg/ffprobe are not installed on the server; cannot mix an episode.")

    meta = episodes.create(
        title=clean_title,
        description=(description or "").strip(),
        language=prepared.language,
        voice_a=prepared.voice_a,
        voice_b=prepared.voice_b,
        chars=prepared.chars,
        blocks=[dict(b) for b in estimate["block_plan"]],
        script=[t.to_dict() for t in prepared.turns],
        brief_data=brief,
    )
    episode_id = meta["id"]

    _ensure_worker()
    _work.put((episode_id, prepared, clean_title))

    return {
        "job_id": episode_id,
        "episode_id": episode_id,
        "state": "queued",
        "queue_position": _queue_depth(),
        "title": clean_title,
        "estimated_chars": estimate["chars"],
        "estimated_blocks": estimate["blocks"],
        "estimated_credits": estimate["estimated_credits"],
        "estimated_duration": estimate["estimated_duration_human"],
        "poll_with": f"podcast_status(job_id='{episode_id}')",
        "note": (
            f"Rendering ~{estimate['blocks']} block(s) sequentially; expect roughly "
            f"{estimate['estimated_duration_human']} of wall clock."
        ),
    }


@mcp.tool()
def podcast_status(job_id: str) -> dict:
    """Progress of a render job. ``job_id`` is what ``podcast_generate`` returned.

    States: ``queued`` → ``validated`` → ``rendering`` (block i/N) → ``mixing``
    → ``done``, or ``failed`` / ``interrupted``. On ``done`` the reply carries
    the episode's path, download URL, duration and size, plus the exact telegram
    skill command to deliver it.
    """
    try:
        meta = episodes.load(job_id)
    except EpisodeError as exc:
        raise ValueError(str(exc)) from None

    progress = meta.get("progress") or {}
    reply: dict[str, Any] = {
        "job_id": meta.get("id"),
        "state": meta.get("state"),
        "progress": progress.get("message"),
        "block": progress.get("block"),
        "blocks": progress.get("blocks"),
        "updated": meta.get("updated"),
    }
    if meta.get("error"):
        reply["error"] = meta["error"]
    if meta.get("state") == "done":
        view = _episode_view(meta)
        reply["episode"] = view
        record = meta.get("delivery") or {}
        if record.get("state") == "sent":
            # Already delivered by the server's own auto-send — telling the
            # agent to send it again would double-post it to the chat.
            reply["telegram"] = {
                "state": "sent", "at": record.get("at"), "chat": record.get("chat")
            }
            reply["deliver"] = (
                "Already delivered to Telegram automatically by the server. "
                "Do not send it again unless the user asks."
            )
        else:
            if record:
                reply["telegram"] = {"state": record.get("state"), "error": record.get("error")}
            reply["deliver"] = _delivery_hint(view)
    return reply


@mcp.tool()
def podcast_list(limit: int = 20) -> dict:
    """Recent episodes, newest first, with the account's remaining character quota.

    Each row carries the episode's ``topic`` and ``angle`` from its brief, so
    this is also the "what have we already covered?" tool — check it before
    planning a new episode, then use ``podcast_get_script`` for the full record
    of any that look related.
    """
    items = episodes.listing(limit=limit)
    reply: dict[str, Any] = {"episodes": items, "count": len(items)}
    if PUBLIC_BASE_URL:
        reply["console"] = (
            f"{PUBLIC_BASE_URL}{BASE_PATH}/{DOWNLOAD_TOKEN}/console"
            if DOWNLOAD_TOKEN
            else f"{PUBLIC_BASE_URL}{CONSOLE_ROUTE}"
        )
    if config.ELEVENLABS_API_KEY:
        try:
            reply["quota"] = tts.subscription()
        except Exception as exc:  # noqa: BLE001 — a quota lookup must not fail a listing
            reply["quota_error"] = str(exc)
    return reply


@mcp.tool()
def podcast_get_script(episode_id: str, include_transcript: bool = False) -> dict:
    """Everything about a past episode: its brief, its config, its script.

    Use this before writing a new episode on a related topic — ``request_yaml``
    is the previous episode's full record (what it was asked to be, which
    sources it used, what settings produced it, what came out), so a follow-up
    can continue rather than repeat.

    The block metadata is the debugging surface: request ids, attempt counts,
    measured duration and chars/second per block, and why each block ended
    (``marker`` = the writer's ``block_break_after``, ``size`` = the character
    limit forced the cut). A seam that sounds wrong is traced from here.

    ``include_transcript`` adds the dialogue as plain readable text.
    """
    try:
        meta = episodes.load(episode_id)
        script = episodes.load_script(episode_id)
    except EpisodeError as exc:
        raise ValueError(str(exc)) from None
    record = _load_record(episode_id, meta)
    reply = {
        "episode_id": meta.get("id"),
        "title": meta.get("title"),
        "state": meta.get("state"),
        "language": meta.get("language"),
        "voice_a": meta.get("voice_a"),
        "voice_b": meta.get("voice_b"),
        "model_id": meta.get("model_id"),
        "chars": meta.get("chars"),
        "brief": meta.get("brief") or {},
        "request_yaml": brief_mod.to_yaml(record),
        "script": script,
        "blocks": meta.get("blocks"),
    }
    if include_transcript:
        reply["transcript"] = brief_mod.script_to_text(
            script, title=meta.get("title", ""), meta=meta
        )
    return reply


@mcp.tool()
def podcast_delete(episode_id: str) -> dict:
    """Delete an episode: its mp3, script, blocks and metadata. Not reversible."""
    try:
        return episodes.delete(episode_id)
    except EpisodeError as exc:
        raise ValueError(str(exc)) from None


@mcp.tool()
def podcast_estimate(script: list[dict], language: str = "en") -> dict:
    """Cost and length of a script without rendering it — validation included.

    Same work as ``podcast_generate(dry_run=True)`` minus the title requirement,
    so it is usable mid-draft to check whether a script is running long.
    """
    try:
        prepared = pipeline.plan(script, language=language)
    except ScriptError as exc:
        raise ValueError(exc.as_text()) from None
    estimate = prepared.estimate()
    return {
        **estimate,
        "language": prepared.language,
        "note": (
            f"~{config.ESTIMATE_CPS} chars/second and {config.CREDITS_PER_CHAR} credits/char, "
            "both measured on English v3 renders — treat as approximate."
        ),
    }


@mcp.tool()
def podcast_voices() -> dict:
    """Voices available on the ElevenLabs account, for ``voice_a`` / ``voice_b``.

    Not part of the original tool surface, but casting the two hosts is
    otherwise a guess: the defaults are only ids in a config file.
    """
    if not config.ELEVENLABS_API_KEY:
        raise ValueError("ELEVENLABS_API_KEY is not configured on the server.")
    try:
        voices = tts.list_voices()
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"could not list voices: {exc}") from None
    return {
        "voices": voices,
        "count": len(voices),
        "defaults": {"HOST_A": config.VOICE_A_ID, "HOST_B": config.VOICE_B_ID},
        "per_language_defaults": {k: {"HOST_A": v[0], "HOST_B": v[1]} for k, v in config.VOICE_MAP.items()},
        "note": (
            "Avoid Professional Voice Clones with eleven_v3 — designed and instant-clone "
            "voices are the reliable choices for dialogue."
        ),
    }


# --------------------------------------------------------------------------
# ASGI app
# --------------------------------------------------------------------------

mcp_asgi = mcp.streamable_http_app()

STATIC_DIR = Path(__file__).resolve().parent / "static"

#: Files an episode exposes over HTTP. meta.json and the raw per-block renders
#: are deliberately absent — the console reads those from disk itself.
_SERVEABLE = {
    "episode.mp3": "audio/mpeg",
    "script.json": "application/json",
    "script.txt": "text/plain; charset=utf-8",
    "request.yaml": "text/yaml; charset=utf-8",
}


def _valid_id(episode_id: str) -> bool:
    try:
        episodes.episode_dir(episode_id)
        return True
    except episodes.EpisodeError:
        return False


def _console_base(request) -> str:
    """The console's own URL prefix, preserving the token segment if one is in use."""
    if DOWNLOAD_TOKEN and request.path_params.get("token") == DOWNLOAD_TOKEN:
        return f"{BASE_PATH}/{DOWNLOAD_TOKEN}/console"
    return CONSOLE_ROUTE


def _console_denied(request) -> bool:
    """True when a token is configured and this request did not present it."""
    return bool(DOWNLOAD_TOKEN) and request.path_params.get("token") != DOWNLOAD_TOKEN


async def console_index(request):
    if _console_denied(request):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    base = _console_base(request)
    items = episodes.listing(limit=500)
    for item in items:
        try:
            meta = episodes.load(item["id"])
            record = brief_mod.build(meta, meta.get("brief"))
            item["summary"] = console.summarize_for_index(meta, record)
        except episodes.EpisodeError:
            item["summary"] = ""
    quota = None
    if config.ELEVENLABS_API_KEY:
        try:
            quota = tts.subscription()
        except Exception as exc:  # noqa: BLE001 — the console must render offline
            logger.info("quota unavailable for console: %s", exc)

    def _count(name: str) -> int:
        try:
            return max(0, min(9999, int(request.query_params.get(name, 0))))
        except (TypeError, ValueError):
            return 0

    return HTMLResponse(
        console.render_index(
            items, base=base, quota=quota,
            deleted=_count("deleted"), failed=_count("failed"),
        )
    )


async def console_episode(request):
    if _console_denied(request):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    base = _console_base(request)
    episode_id = request.path_params["episode_id"]
    try:
        meta = episodes.load(episode_id)
    except episodes.EpisodeError:
        return HTMLResponse(
            console.render_error("No such episode.", base=base), status_code=404
        )
    try:
        script = episodes.load_script(episode_id)
    except episodes.EpisodeError:
        script = []
    record = _load_record(episode_id, meta)
    directory = episodes.episode_dir(episode_id)
    available = {name for name in _SERVEABLE if (directory / name).is_file()}
    return HTMLResponse(
        console.render_episode(
            meta, script, record, base=base, available=available,
            telegram_ready=settings.delivery_ready()[0],
        )
    )


async def console_settings(request):
    """View or update the Telegram delivery settings."""
    if _console_denied(request):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    base = _console_base(request)
    saved = tested = error = ""

    if request.method == "POST":
        form = await request.form()
        settings.save(
            telegram_bot_token=str(form.get("telegram_bot_token") or ""),
            telegram_chat_id=str(form.get("telegram_chat_id") or ""),
            # An unchecked checkbox is simply absent from the form body.
            telegram_auto_send=bool(form.get("telegram_auto_send")),
        )
        saved = True
        if str(form.get("action")) == "test":
            try:
                result = delivery.check()
                tested = result["detail"]
            except delivery.DeliveryError as exc:
                error = str(exc)
        return HTMLResponse(
            console.render_settings(
                settings.public_view(), base=base, saved=saved, tested=tested, error=error
            )
        )

    return HTMLResponse(console.render_settings(settings.public_view(), base=base))


async def console_send(request):
    """Manually deliver one finished episode to Telegram."""
    if _console_denied(request):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    base = _console_base(request)
    episode_id = request.path_params["episode_id"]
    try:
        pipeline.deliver(episode_id)
    except (episodes.EpisodeError, delivery.DeliveryError) as exc:
        logger.warning("manual send failed for %s: %s", episode_id, exc)
    # Either way the outcome is recorded on the episode, so redirect back to it
    # and let the page show what happened.
    return RedirectResponse(f"{base}/{episode_id}", status_code=303)


async def console_delete_selected(request):
    """Delete every checked episode, then redirect with a count.

    Partial failure is normal and not an error: an episode can finish rendering,
    or be deleted in another tab, between the page render and the submit. Each
    id is attempted independently and the outcome is reported.
    """
    if _console_denied(request):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    base = _console_base(request)
    form = await request.form()
    ids = [str(v) for v in form.getlist("id")]
    deleted = failed = 0
    for episode_id in ids:
        try:
            episodes.delete(episode_id)
            deleted += 1
        except episodes.EpisodeError as exc:
            failed += 1
            logger.info("bulk delete skipped %s: %s", episode_id, exc)
    if ids:
        logger.info("bulk delete: %d removed, %d skipped", deleted, failed)
    # POST-redirect-GET: reloading the result page must not re-run the delete.
    return RedirectResponse(f"{base}?deleted={deleted}&failed={failed}", status_code=303)


async def console_delete(request):
    if _console_denied(request):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    base = _console_base(request)
    episode_id = request.path_params["episode_id"]
    try:
        episodes.delete(episode_id)
    except episodes.EpisodeError as exc:
        # 404 for "no such episode", 409 for "exists but is mid-render" — the
        # second is retryable once the worker finishes, the first never is.
        exists = episodes.meta_path(episode_id).is_file() if _valid_id(episode_id) else False
        return HTMLResponse(
            console.render_error(str(exc), base=base), status_code=409 if exists else 404
        )
    # POST-redirect-GET: a reload must not re-post a delete.
    return RedirectResponse(base, status_code=303)


async def serve_static(request):
    """Console CSS and the self-hosted fonts. Public: no secrets, no episode data."""
    parts = [p for p in request.path_params["path"].split("/") if p not in ("", ".", "..")]
    if not parts:
        return JSONResponse({"detail": "Not found"}, status_code=404)
    target = (STATIC_DIR.joinpath(*parts)).resolve()
    if STATIC_DIR.resolve() not in target.parents or not target.is_file():
        return JSONResponse({"detail": "Not found"}, status_code=404)
    return FileResponse(target, headers={"Cache-Control": "public, max-age=604800"})


async def serve_episode(request):
    """Serve an episode's mp3 or script.

    Deliberately not a StaticFiles mount: that would also expose meta.json and
    every per-block render. Only the two files a listener or a feed needs.
    """
    episode_id = request.path_params["episode_id"]
    filename = request.path_params["filename"]
    if DOWNLOAD_TOKEN and request.path_params.get("token") != DOWNLOAD_TOKEN:
        return JSONResponse({"detail": "Not found"}, status_code=404)
    media_type = _SERVEABLE.get(filename)
    if not media_type:
        return JSONResponse({"detail": "Not found"}, status_code=404)
    try:
        base = episodes.episode_dir(episode_id).resolve()
        target = (base / filename).resolve()
        if base not in target.parents or not target.is_file():
            raise EpisodeError("not found")
    except (EpisodeError, OSError):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    return FileResponse(
        target,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )


async def health_check(request):
    # Deliberately no quota lookup here: Docker polls this every 30s and it
    # must not depend on (or hammer) the ElevenLabs API. Quota lives in
    # podcast_list.
    return JSONResponse(
        {
            "status": "healthy",
            "service": _safe_name,
            "elevenlabs_configured": bool(config.ELEVENLABS_API_KEY),
            "ffmpeg": mixing.available(),
            "model_id": config.MODEL_ID,
            "block_char_limit": config.BLOCK_CHAR_LIMIT,
            "queue_depth": _queue_depth(),
            "episodes": len(episodes.listing(limit=1000)),
            "public_base_url": PUBLIC_BASE_URL,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
    )


@contextlib.asynccontextmanager
async def lifespan(_: Starlette):
    episodes.root().mkdir(parents=True, exist_ok=True)
    # A job that was rendering when the process died must not keep claiming
    # progress that no worker is making (spec §8: status never lies).
    with contextlib.suppress(Exception):
        episodes.reap_interrupted()
    if config.EPISODE_MAX_AGE_DAYS > 0:
        with contextlib.suppress(Exception):
            pruned = episodes.prune(config.EPISODE_MAX_AGE_DAYS)
            if pruned:
                logger.info("pruned %d episode(s) older than %d days",
                            pruned, config.EPISODE_MAX_AGE_DAYS)
    _ensure_worker()
    async with mcp.session_manager.run():
        yield


routes = [
    Route("/health", health_check, methods=["GET"]),
    Route(f"{BASE_PATH}/health", health_check, methods=["GET"]),
    Route(f"{CONSOLE_ROUTE}/assets/{{path:path}}", serve_static, methods=["GET"]),
    Route(f"{EPISODES_ROUTE}/{{episode_id}}/{{filename}}", serve_episode, methods=["GET"]),
]

# Both URL shapes exist so the console works identically with and without a
# DOWNLOAD_TOKEN: bare paths locally, token-prefixed paths on the VPS. The
# token-prefixed routes are only registered when a token is actually set, so an
# unconfigured server cannot serve a guessable "/podcast/<anything>/console".
_console_shapes = [(CONSOLE_ROUTE, "")]
if DOWNLOAD_TOKEN:
    _console_shapes.append((f"{BASE_PATH}/{{token}}/console", f"{BASE_PATH}/{{token}}"))
    routes.append(
        Route(
            f"{BASE_PATH}/{{token}}/episodes/{{episode_id}}/{{filename}}",
            serve_episode,
            methods=["GET"],
        )
    )

for _console_path, _prefix in _console_shapes:
    # The assets route MUST precede {episode_id}/{filename}: Starlette matches
    # in order, and "assets/console.css" is a perfectly good (episode_id,
    # filename) pair — which silently 404s the stylesheet for the whole console.
    if _prefix:
        routes.append(Route(f"{_prefix}/console/assets/{{path:path}}", serve_static, methods=["GET"]))
    routes += [
        Route(_console_path, console_index, methods=["GET"]),
        # Before the {episode_id} routes for the same reason as assets above:
        # "delete-selected" would otherwise be matched as an episode id.
        Route(f"{_console_path}/delete-selected", console_delete_selected, methods=["POST"]),
        Route(f"{_console_path}/settings", console_settings, methods=["GET", "POST"]),
        Route(f"{_console_path}/{{episode_id}}", console_episode, methods=["GET"]),
        Route(f"{_console_path}/{{episode_id}}/delete", console_delete, methods=["POST"]),
        Route(f"{_console_path}/{{episode_id}}/send", console_send, methods=["POST"]),
        # Download links on an episode page resolve relative to the console
        # base, so the same files are reachable under the console prefix too.
        Route(f"{_console_path}/{{episode_id}}/{{filename}}", serve_episode, methods=["GET"]),
    ]

routes.append(Mount("/", app=mcp_asgi))

app = Starlette(routes=routes, lifespan=lifespan)


class TokenAuthMiddleware(BaseHTTPMiddleware):
    """Token gate for everything under BASE_PATH.

    Accepts ``Authorization: Bearer <token>``, ``?token=`` and
    ``/podcast/<token>/…`` path tokens — a claude.ai custom connector can only
    carry the secret in the URL. Health and episode routes stay public: the
    episode route has its own DOWNLOAD_TOKEN, and Telegram fetching an episode
    by URL cannot send a bearer header either.

    MCP_REQUIRE_AUTH=true is what makes a public deployment safe: without it an
    empty MCP_TOKENS means "no auth", which is only acceptable on localhost.
    """

    def __init__(self, app):
        super().__init__(app)
        self.allowed_tokens = set(_TOKENS)
        self.allow_url_tokens = os.getenv("MCP_ALLOW_URL_TOKENS", "true").lower() in (
            "1", "true", "yes",
        )
        self.require_auth = os.getenv("MCP_REQUIRE_AUTH", "").lower() in ("1", "true", "yes")
        if self.require_auth and not self.allowed_tokens:
            logger.warning("MCP_REQUIRE_AUTH=true but MCP_TOKENS is empty -> rejecting everything")
        elif not self.allowed_tokens:
            logger.warning("MCP_TOKENS empty -> token auth DISABLED for %s", BASE_PATH)

    def _is_public(self, path: str) -> bool:
        """Routes the MCP token gate does not apply to.

        "Public" here means "not guarded by MCP_TOKENS" — not unguarded. The
        console and the episode files carry their own DOWNLOAD_TOKEN, checked in
        their handlers, because a browser following a link and Telegram fetching
        an mp3 both arrive without an Authorization header.
        """
        if path in ("/health", f"{BASE_PATH}/health"):
            return True
        if path.startswith((EPISODES_ROUTE, CONSOLE_ROUTE)):
            return True
        # /podcast/<download-token>/{episodes,console}/... — its own secret.
        if DOWNLOAD_TOKEN:
            segments = [s for s in path.split("/") if s]
            if (
                len(segments) >= 3
                and segments[0] == _safe_name
                and segments[1] == DOWNLOAD_TOKEN
                and segments[2] in ("episodes", "console")
            ):
                return True
        return False

    def _strip_token_segment(self, request, path, token=None) -> bool:
        """Rewrite /podcast/<seg>/... -> /podcast/... so the MCP app sees its own path."""
        segments = [s for s in path.split("/") if s]
        if len(segments) < 2 or segments[0] != _safe_name:
            return False
        if token is not None and segments[1] != token:
            return False
        remainder = "/".join([_safe_name] + segments[2:])
        new_path = "/" + (
            remainder + "/" if path.endswith("/") and not remainder.endswith("/") else remainder
        )
        if new_path == BASE_PATH:
            new_path = STREAM_PATH
        request.scope["path"] = new_path
        if "raw_path" in request.scope:
            request.scope["raw_path"] = new_path.encode("utf-8")
        return True

    async def dispatch(self, request, call_next):
        path = request.url.path or "/"
        if not path.startswith(BASE_PATH) or self._is_public(path):
            return await call_next(request)

        async def proceed(source):
            logger.info("Authenticated %s %s via %s", request.method, path, source)
            return await call_next(request)

        if not self.allowed_tokens:
            if self.require_auth:
                return JSONResponse(
                    {"detail": "Unauthorized"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
            self._strip_token_segment(request, path)
            return await call_next(request)

        auth = request.headers.get("authorization")
        token = (
            auth.split(" ", 1)[1].strip()
            if auth and auth.lower().startswith("bearer ")
            else None
        )
        if token and token in self.allowed_tokens:
            return await proceed("header")

        if self.allow_url_tokens:
            url_token = request.query_params.get("token")
            if url_token and url_token in self.allowed_tokens:
                return await proceed("query")
            segments = [s for s in path.split("/") if s]
            if len(segments) >= 2 and segments[0] == _safe_name and segments[1] in self.allowed_tokens:
                self._strip_token_segment(request, path, segments[1])
                return await proceed("path")

        return JSONResponse(
            {"detail": "Unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )


app.add_middleware(TokenAuthMiddleware)


def main():
    logger.info(
        "Starting %s MCP on port %s at %s (model %s, block limit %d chars)",
        MCP_NAME, PORT, STREAM_PATH, config.MODEL_ID, config.BLOCK_CHAR_LIMIT,
    )
    uvicorn.run(
        app=app,
        host=os.getenv("HOST", "0.0.0.0"),
        port=PORT,
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
        access_log=True,
        log_config=None,  # let access logs reach the root handler's token redaction
        proxy_headers=True,
        forwarded_allow_ips="*",
        timeout_keep_alive=660,
    )


if __name__ == "__main__":
    main()
