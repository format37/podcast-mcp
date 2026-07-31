"""Environment-driven configuration for the podcast renderer.

Every knob is read at import time so both entry points — the ``render.py`` CLI
and the MCP server — see the same values, and so a bad value fails loudly at
startup instead of halfway through a paid render.

The character limits deserve a note. The spec drafted against a 3,000-char
text-to-dialogue budget; the API actually documents **2,000 characters summed
across all ``inputs[].text``** per request. BLOCK_CHAR_LIMIT therefore defaults
to 1,800 rather than the spec's 2,500 — the same safety-margin ratio applied to
the real ceiling.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Hard API ceiling, not a preference: the endpoint rejects (or silently mangles)
# requests whose inputs sum past this. BLOCK_CHAR_LIMIT is validated against it.
API_CHAR_CEILING = 2000


def _int(name: str, default: int, minimum: int = 1, maximum: int | None = None) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        logger.warning("%s=%r is not a number; using %s", name, raw, default)
        return default
    if value < minimum:
        logger.warning("%s=%s is below the minimum %s; clamping", name, value, minimum)
        return minimum
    if maximum is not None and value > maximum:
        logger.warning("%s=%s is above the maximum %s; clamping", name, value, maximum)
        return maximum
    return value


def _float(name: str, default: float, minimum: float = 0.0, maximum: float | None = None) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("%s=%r is not a number; using %s", name, raw, default)
        return default
    if value < minimum:
        return minimum
    if maximum is not None and value > maximum:
        return maximum
    return value


def _str(name: str, default: str = "") -> str:
    return (os.getenv(name) or "").strip() or default


def _bool(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------
# ElevenLabs
# --------------------------------------------------------------------------

ELEVENLABS_API_KEY = _str("ELEVENLABS_API_KEY")
API_BASE = _str("ELEVENLABS_API_BASE", "https://api.elevenlabs.io")
MODEL_ID = _str("MODEL_ID", "eleven_v3")
OUTPUT_FORMAT = _str("ELEVENLABS_OUTPUT_FORMAT", "mp3_44100_128")
STABILITY = _float("ELEVENLABS_STABILITY", 0.5, 0.0, 1.0)
REQUEST_TIMEOUT_S = _float("ELEVENLABS_TIMEOUT_S", 300.0, 10.0, 900.0)

# Default host voices. Both are premade multilingual voices, so the same pair
# works for en/ru/ja; per-language overrides live in VOICE_MAP below.
VOICE_A_ID = _str("VOICE_A_ID", "cgSgspJ2msm6clMCkdW9")  # Jessica — playful, bright
VOICE_B_ID = _str("VOICE_B_ID", "cjVigY5qzO86Huf0OWal")  # Eric — smooth, trustworthy

#: ``VOICE_MAP=ru:idA|idB,ja:idA|idB`` — per-language default pairs (spec §7).
def _parse_voice_map(raw: str) -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or ":" not in entry:
            continue
        lang, _, pair = entry.partition(":")
        a, _, b = pair.partition("|")
        if a.strip() and b.strip():
            out[lang.strip().lower()] = (a.strip(), b.strip())
        else:
            logger.warning("VOICE_MAP entry %r is not 'lang:voiceA|voiceB'; ignored", entry)
    return out


VOICE_MAP = _parse_voice_map(_str("VOICE_MAP"))

# --------------------------------------------------------------------------
# Script + chunking
# --------------------------------------------------------------------------

MAX_SCRIPT_CHARS = _int("MAX_SCRIPT_CHARS", 30000, 100)
MAX_TURN_CHARS = _int("MAX_TURN_CHARS", 500, 10)
BLOCK_CHAR_LIMIT = _int("BLOCK_CHAR_LIMIT", 1800, 100, API_CHAR_CEILING)
#: A ``block_break_after`` marker is honoured only once the block has at least
#: this many characters. Without it, a script that marks every turn would render
#: one API call per turn: maximum seams, maximum latency, worst prosody.
MIN_BLOCK_CHARS = _int("MIN_BLOCK_CHARS", 600, 0)

SPEAKERS = ("HOST_A", "HOST_B")

# --------------------------------------------------------------------------
# Render loop
# --------------------------------------------------------------------------

MAX_BLOCK_ATTEMPTS = _int("MAX_BLOCK_ATTEMPTS", 3, 1, 10)
#: Backoff budget for 429/5xx across a single block (spec §8: max 5 min).
RATE_LIMIT_MAX_WAIT_S = _float("RATE_LIMIT_MAX_WAIT_S", 300.0, 0.0, 1800.0)

#: Characters per second, per language (spec §10 Q2 — measured, not guessed).
#: This MUST be per-language: a character of Japanese carries far more speech
#: than a character of English, and a single global window silently turns every
#: Japanese render into three paid retries and a failed job.
#:
#:   en 14.1   three blocks at 13.7 / 13.7 / 14.5
#:   ru 13.2   one block
#:   ja  5.6   one block — 2.5x denser than English
#:
#: Override with LANG_CPS="ja:5.6,ru:13.2,de:14".
def _parse_lang_cps(raw: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or ":" not in entry:
            continue
        lang, _, value = entry.partition(":")
        try:
            rate = float(value)
        except (TypeError, ValueError):
            logger.warning("LANG_CPS entry %r is not 'lang:rate'; ignored", entry)
            continue
        if rate > 0:
            out[lang.strip().lower()] = rate
    return out


LANG_CPS = _parse_lang_cps(_str("LANG_CPS", "en:14.1,ru:13.2,ja:5.6"))

#: Fallback rate for a language nobody has measured yet.
ESTIMATE_CPS = _float("ESTIMATE_CPS", 14.1, 1.0)

#: The sanity window is derived from the language's own rate rather than set in
#: absolute terms, so adding a language means adding one number. The factors are
#: wide on purpose: this is a gross-failure detector (truncation, silence,
#: looping), not a quality gate. English 14.1 c/s => accept 5.9–31 c/s.
SANITY_LOW_FACTOR = _float("SANITY_LOW_FACTOR", 0.42, 0.05, 1.0)
SANITY_HIGH_FACTOR = _float("SANITY_HIGH_FACTOR", 2.2, 1.0, 20.0)

#: For an unmeasured language the rate is genuinely unknown, so the window
#: widens to "is this audio at all" rather than pretending to a calibration we
#: do not have.
SANITY_UNKNOWN_MIN_CPS = _float("SANITY_UNKNOWN_MIN_CPS", 2.0, 0.1)
SANITY_UNKNOWN_MAX_CPS = _float("SANITY_UNKNOWN_MAX_CPS", 35.0, 1.0)

#: Credits billed per character of script. Measured, not documented: a
#: 2,986-char script moved the account counter by 1,642 (0.55x). ElevenLabs
#: publishes no v3 dialogue rate, so this is an empirical constant that may
#: change under us — hence a knob, and hence estimates are labelled approximate.
CREDITS_PER_CHAR = _float("CREDITS_PER_CHAR", 0.55, 0.01)

# --------------------------------------------------------------------------
# Mixing
# --------------------------------------------------------------------------

#: "crossfade" overlaps block joins by CROSSFADE_MS; "pad" inserts
#: PAD_SILENCE_MS of silence instead (spec §5 fallback if crossfade smears words).
CONCAT_MODE = _str("CONCAT_MODE", "crossfade").lower()
CROSSFADE_MS = _int("CROSSFADE_MS", 60, 1, 2000)
PAD_SILENCE_MS = _int("PAD_SILENCE_MS", 100, 0, 5000)

LOUDNORM_I = _float("LOUDNORM_I", -16.0, -40.0, 0.0)  # podcast standard
LOUDNORM_TP = _float("LOUDNORM_TP", -1.5, -9.0, 0.0)
LOUDNORM_LRA = _float("LOUDNORM_LRA", 11.0, 1.0, 20.0)

MP3_BITRATE = _str("MP3_BITRATE", "96k")
MP3_SAMPLE_RATE = _int("MP3_SAMPLE_RATE", 44100, 8000)
MP3_CHANNELS = _int("MP3_CHANNELS", 1, 1, 2)

INTRO_PATH = _str("INTRO_PATH")
OUTRO_PATH = _str("OUTRO_PATH")
JINGLE_GAIN_DB = _float("JINGLE_GAIN_DB", -6.0, -60.0, 0.0)

FFMPEG_BIN = _str("FFMPEG_BIN", "ffmpeg")
FFPROBE_BIN = _str("FFPROBE_BIN", "ffprobe")
FFMPEG_TIMEOUT_S = _float("FFMPEG_TIMEOUT_S", 600.0, 30.0, 3600.0)

# --------------------------------------------------------------------------
# Storage / show identity
# --------------------------------------------------------------------------

# Resolved to an absolute path on purpose: the episode path is handed to the
# agent to feed the telegram skill, which runs from a different working
# directory. A relative path here becomes a file-not-found there.
OUTPUT_DIR = Path(_str("OUTPUT_DIR", "/app/data/episodes")).expanduser().resolve()

#: Where OUTPUT_DIR is visible from *outside* the container. Delivery is
#: agent-side: the agent runs the telegram skill on the host and needs a path it
#: can actually open, but the server only knows its own /app/data view of the
#: bind mount. Set this to the host side of that mount and every reported path
#: is translated. Unset (VPS, or running uncontainerised) it is a no-op.
HOST_OUTPUT_DIR = _str("HOST_OUTPUT_DIR")
#: Keep per-block mp3s after a successful mix? They are the raw material for
#: debugging a bad seam, at roughly the cost of the episode itself.
KEEP_BLOCKS = _bool("KEEP_BLOCKS", True)
EPISODE_MAX_AGE_DAYS = _int("EPISODE_MAX_AGE_DAYS", 0, 0)  # 0 = never prune

SHOW_TITLE = _str("SHOW_TITLE", "Podcast")
SHOW_IMAGE = _str("SHOW_IMAGE")


def rates_for(language: str | None) -> tuple[float, float, float, bool]:
    """``(estimate_cps, sanity_min_cps, sanity_max_cps, calibrated)`` for a language."""
    lang = (language or "").strip().lower()
    rate = LANG_CPS.get(lang)
    if rate is None:
        return ESTIMATE_CPS, SANITY_UNKNOWN_MIN_CPS, SANITY_UNKNOWN_MAX_CPS, False
    return rate, rate * SANITY_LOW_FACTOR, rate * SANITY_HIGH_FACTOR, True


def host_path(path: str | Path) -> str:
    """Translate a path inside OUTPUT_DIR to its host-visible equivalent."""
    text = str(path)
    if not HOST_OUTPUT_DIR:
        return text
    base = str(OUTPUT_DIR)
    if text.startswith(base):
        return HOST_OUTPUT_DIR.rstrip("/") + text[len(base):]
    return text


def voices_for(language: str | None, voice_a: str | None, voice_b: str | None) -> tuple[str, str]:
    """Resolve the voice pair: explicit override > per-language map > global default."""
    lang = (language or "").strip().lower()
    mapped = VOICE_MAP.get(lang)
    default_a, default_b = mapped if mapped else (VOICE_A_ID, VOICE_B_ID)
    return (voice_a or default_a), (voice_b or default_b)
