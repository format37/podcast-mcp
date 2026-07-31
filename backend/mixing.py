"""ffmpeg stage: block mp3s in, one normalised episode out (spec §5).

Three jobs, in order:

* **Join** the blocks. Either a short ``acrossfade`` (default) or a padded
  concat — the spec leaves the choice to the ear, so it is one env var.
* **Normalise** to broadcast loudness with two-pass ``loudnorm``. One pass is a
  guess; two passes measure the file and then hit the target, which matters when
  separate v3 generations come back at visibly different levels.
* **Encode** mono 96 kbps, which keeps a 25-minute episode near 10 MB — under
  Telegram's 20 MB send-by-URL ceiling, so delivery takes the simple path.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

import config

logger = logging.getLogger(__name__)


class MixError(RuntimeError):
    """ffmpeg/ffprobe failed, or is not installed."""


def _run(cmd: list[str], *, what: str) -> subprocess.CompletedProcess:
    logger.debug("%s: %s", what, " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=config.FFMPEG_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError as exc:
        raise MixError(
            f"{cmd[0]} is not installed or not on PATH — {what} needs ffmpeg."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise MixError(f"{what} timed out after {config.FFMPEG_TIMEOUT_S:.0f}s") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-8:]
        raise MixError(f"{what} failed (exit {proc.returncode}): " + " / ".join(tail))
    return proc


def available() -> bool:
    return bool(shutil.which(config.FFMPEG_BIN) and shutil.which(config.FFPROBE_BIN))


def duration_s(path: Path) -> float:
    """Decoded duration in seconds. Used for the per-block sanity check (spec §5.5)."""
    proc = _run(
        [
            config.FFPROBE_BIN,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json",
            str(path),
        ],
        what=f"ffprobe {path.name}",
    )
    try:
        value = json.loads(proc.stdout)["format"]["duration"]
        return float(value)
    except (KeyError, ValueError, TypeError) as exc:
        raise MixError(f"ffprobe could not read a duration from {path.name}") from exc


def _concat_filter(count: int, first_label: str = "") -> tuple[str, str]:
    """Build the filter graph that joins ``count`` inputs; returns (filter, out_label)."""
    if config.CONCAT_MODE == "crossfade":
        # acrossfade takes exactly two inputs, so N blocks chain N-1 of them.
        seconds = config.CROSSFADE_MS / 1000.0
        parts = []
        prev = "[0:a]"
        for i in range(1, count):
            out = f"[x{i}]"
            parts.append(f"{prev}[{i}:a]acrossfade=d={seconds}:c1=tri:c2=tri{out}")
            prev = out
        return ";".join(parts), prev
    # Padded concat: a sliver of silence after every block but the last. Blunter
    # than a crossfade but it can never smear a word across the join.
    pad = config.PAD_SILENCE_MS / 1000.0
    parts = []
    labels = []
    for i in range(count):
        if pad > 0 and i < count - 1:
            parts.append(f"[{i}:a]apad=pad_dur={pad}[p{i}]")
            labels.append(f"[p{i}]")
        else:
            labels.append(f"[{i}:a]")
    parts.append(f"{''.join(labels)}concat=n={count}:v=0:a=1[cat]")
    return ";".join(parts), "[cat]"


def _measure_loudness(source: Path) -> dict[str, str] | None:
    """First loudnorm pass: measure. Returns None if ffmpeg's report is unusable."""
    proc = _run(
        [
            config.FFMPEG_BIN, "-hide_banner", "-nostats", "-i", str(source),
            "-af",
            f"loudnorm=I={config.LOUDNORM_I}:TP={config.LOUDNORM_TP}:"
            f"LRA={config.LOUDNORM_LRA}:print_format=json",
            "-f", "null", "-",
        ],
        what="loudnorm measurement pass",
    )
    stderr = proc.stderr or ""
    start = stderr.rfind("{")
    end = stderr.rfind("}")
    if start == -1 or end <= start:
        logger.warning("loudnorm measurement produced no JSON; falling back to single-pass")
        return None
    try:
        measured = json.loads(stderr[start : end + 1])
    except ValueError:
        logger.warning("loudnorm measurement JSON was malformed; falling back to single-pass")
        return None
    required = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")
    if not all(k in measured for k in required):
        return None
    # A silent or near-silent input measures as -inf, which loudnorm refuses as
    # a linear-mode parameter. Fall back rather than fail the whole episode.
    if any("inf" in str(measured[k]).lower() for k in required):
        logger.warning("loudnorm measured -inf (near-silent input); falling back to single-pass")
        return None
    return measured


def _loudnorm_filter(measured: dict[str, str] | None) -> str:
    base = f"loudnorm=I={config.LOUDNORM_I}:TP={config.LOUDNORM_TP}:LRA={config.LOUDNORM_LRA}"
    if not measured:
        return base
    return (
        f"{base}:measured_I={measured['input_i']}:measured_TP={measured['input_tp']}"
        f":measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}"
        f":offset={measured['target_offset']}:linear=true:print_format=summary"
    )


def _jingle_inputs(blocks: list[Path]) -> list[Path]:
    """Wrap the blocks in the configured intro/outro, if any exist."""
    sequence: list[Path] = []
    intro = Path(config.INTRO_PATH) if config.INTRO_PATH else None
    outro = Path(config.OUTRO_PATH) if config.OUTRO_PATH else None
    if intro and intro.is_file():
        sequence.append(intro)
    elif intro:
        logger.warning("INTRO_PATH %s does not exist; skipping intro", intro)
    sequence.extend(blocks)
    if outro and outro.is_file():
        sequence.append(outro)
    elif outro:
        logger.warning("OUTRO_PATH %s does not exist; skipping outro", outro)
    return sequence


def mix(blocks: list[Path], destination: Path, *, work_dir: Path | None = None) -> dict[str, float]:
    """Join, normalise and encode ``blocks`` into ``destination``.

    Returns ``{"duration_s", "size_bytes"}`` for the finished episode.
    """
    if not blocks:
        raise MixError("no blocks to mix")
    missing = [str(b) for b in blocks if not b.is_file()]
    if missing:
        raise MixError(f"block file(s) missing: {', '.join(missing)}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    work_dir = work_dir or destination.parent
    work_dir.mkdir(parents=True, exist_ok=True)

    sequence = _jingle_inputs(blocks)
    joined = work_dir / "_joined.wav"

    # Join into an intermediate WAV: loudnorm gets to measure and re-encode from
    # lossless material rather than stacking a second mp3 generation on top.
    cmd = [config.FFMPEG_BIN, "-hide_banner", "-nostats", "-y"]
    for item in sequence:
        cmd += ["-i", str(item)]

    if len(sequence) == 1:
        cmd += ["-map", "0:a"]
    else:
        graph, out_label = _concat_filter(len(sequence))
        # Jingles are usually mastered louder than speech; duck them so the
        # intro does not blow the listener's ears off before loudnorm averages.
        if config.JINGLE_GAIN_DB and len(sequence) != len(blocks):
            gain = 10 ** (config.JINGLE_GAIN_DB / 20.0)
            pre = []
            for i, item in enumerate(sequence):
                if item not in blocks:
                    pre.append(f"[{i}:a]volume={gain:.4f}[g{i}]")
            if pre:
                # Re-label the ducked inputs inside the join graph.
                for i, item in enumerate(sequence):
                    if item not in blocks:
                        graph = graph.replace(f"[{i}:a]", f"[g{i}]")
                graph = ";".join(pre) + ";" + graph
        cmd += ["-filter_complex", graph, "-map", out_label]

    cmd += ["-ac", str(config.MP3_CHANNELS), "-ar", str(config.MP3_SAMPLE_RATE), str(joined)]
    _run(cmd, what="joining blocks")

    try:
        measured = _measure_loudness(joined)
        _run(
            [
                config.FFMPEG_BIN, "-hide_banner", "-nostats", "-y",
                "-i", str(joined),
                "-af", _loudnorm_filter(measured),
                "-ac", str(config.MP3_CHANNELS),
                "-ar", str(config.MP3_SAMPLE_RATE),
                "-c:a", "libmp3lame",
                "-b:a", config.MP3_BITRATE,
                str(destination),
            ],
            what="loudness normalisation + mp3 encode",
        )
    finally:
        joined.unlink(missing_ok=True)

    return {
        "duration_s": round(duration_s(destination), 2),
        "size_bytes": destination.stat().st_size,
    }


def tag(path: Path, *, title: str, artist: str, album: str = "", cover: Path | None = None) -> None:
    """Write ID3 tags in place — Telegram reads them for the inline player's labels."""
    tagged = path.with_suffix(".tagged.mp3")
    cmd = [config.FFMPEG_BIN, "-hide_banner", "-nostats", "-y", "-i", str(path)]
    if cover and cover.is_file():
        cmd += ["-i", str(cover), "-map", "0:a", "-map", "1:v", "-c:v", "copy",
                "-metadata:s:v", "title=Album cover",
                "-metadata:s:v", "comment=Cover (front)",
                "-disposition:v", "attached_pic"]
    else:
        cmd += ["-map", "0:a"]
    cmd += ["-c:a", "copy", "-metadata", f"title={title}", "-metadata", f"artist={artist}"]
    if album:
        cmd += ["-metadata", f"album={album}"]
    cmd += ["-id3v2_version", "3", str(tagged)]
    try:
        _run(cmd, what="writing ID3 tags")
    except MixError as exc:
        # Cosmetic: an untagged episode still plays. Never fail a paid render here.
        logger.warning("could not tag %s: %s", path.name, exc)
        tagged.unlink(missing_ok=True)
        return
    tagged.replace(path)
