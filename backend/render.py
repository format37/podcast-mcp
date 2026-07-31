#!/usr/bin/env python3
"""``render.py script.json out.mp3`` — the pipeline without the server (spec M1).

Useful on its own for spikes and calibration runs, and it is the fastest way to
answer "is this seam acceptable?" without a job queue in the way::

    python render.py script.json episode.mp3
    python render.py script.json episode.mp3 --dry-run     # plan + cost, no API calls
    python render.py script.json episode.mp3 --language ru --voice-a <id> --voice-b <id>
    python render.py script.json episode.mp3 --keep-blocks-dir ./blocks
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv

# Load .env before config reads the environment.
for candidate in (Path(".env"), Path(__file__).resolve().parent / ".env",
                  Path(__file__).resolve().parent.parent / ".env"):
    if candidate.is_file():
        load_dotenv(candidate)
        break

import config  # noqa: E402
import mixing  # noqa: E402
import pipeline  # noqa: E402
import requests  # noqa: E402
import script_model  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="render.py",
        description="Render a two-host dialogue script to a finished podcast mp3.",
    )
    parser.add_argument("script", type=Path, help="JSON array of turns (see docs/tech-specs.md §4)")
    parser.add_argument("output", type=Path, nargs="?", help="destination mp3")
    parser.add_argument("--language", default="en", help="ISO 639-1 code, default en")
    parser.add_argument("--voice-a", default=None, help="voice_id for HOST_A")
    parser.add_argument("--voice-b", default=None, help="voice_id for HOST_B")
    parser.add_argument("--title", default=None, help="ID3 title, default: output filename")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate, chunk and cost the script without calling the API",
    )
    parser.add_argument(
        "--keep-blocks-dir",
        type=Path,
        default=None,
        help="write per-block mp3s here instead of a temp dir (for judging seams)",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    if not args.script.is_file():
        print(f"error: no such script file: {args.script}", file=sys.stderr)
        return 2
    try:
        raw = json.loads(args.script.read_text(encoding="utf-8"))
    except ValueError as exc:
        print(f"error: {args.script} is not valid JSON: {exc}", file=sys.stderr)
        return 2

    try:
        prepared = pipeline.plan(
            raw, language=args.language, voice_a=args.voice_a, voice_b=args.voice_b
        )
    except script_model.ScriptError as exc:
        print(f"error: {exc.as_text()}", file=sys.stderr)
        return 1

    estimate = prepared.estimate()
    print(
        f"{estimate['turns']} turns · {estimate['chars']} chars · "
        f"{estimate['blocks']} block(s) · ~{estimate['estimated_duration_human']} audio · "
        f"~{estimate['estimated_credits']} credits"
    )
    for block in estimate["block_plan"]:
        print(
            f"  block {block['index']:>2}: {block['chars']:>5} chars, "
            f"{block['turns']:>2} turns, cut={block['boundary']:<6} “{block['first_line']}…”"
        )

    if args.dry_run:
        return 0
    if args.output is None:
        print("error: an output path is required unless --dry-run is given", file=sys.stderr)
        return 2
    if not mixing.available():
        print("error: ffmpeg and ffprobe must be installed to mix an episode", file=sys.stderr)
        return 3

    with tempfile.TemporaryDirectory(prefix="podcast-blocks-") as tmp:
        block_dir = args.keep_blocks_dir or Path(tmp)
        block_dir.mkdir(parents=True, exist_ok=True)
        rendered: list[Path] = []
        try:
            with requests.Session() as session:
                for block in prepared.blocks:
                    print(
                        f"rendering block {block.index + 1}/{len(prepared.blocks)} "
                        f"({block.chars} chars)…",
                        flush=True,
                    )
                    destination = block_dir / f"{block.index:03d}.mp3"
                    info = pipeline.render_block_to_file(
                        block,
                        destination,
                        voice_a=prepared.voice_a,
                        voice_b=prepared.voice_b,
                        language=prepared.language,
                        session=session,
                    )
                    print(
                        f"  ok: {info['duration_s']}s, {info['chars_per_second']} chars/s, "
                        f"{info['attempts']} attempt(s), request {info['request_id']}"
                    )
                    rendered.append(destination)
        except pipeline.RenderFailed as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

        print("mixing…", flush=True)
        try:
            stats = mixing.mix(rendered, args.output)
            mixing.tag(
                args.output,
                title=args.title or args.output.stem,
                artist=config.SHOW_TITLE,
                album=config.SHOW_TITLE,
                cover=Path(config.SHOW_IMAGE) if config.SHOW_IMAGE else None,
            )
        except mixing.MixError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    size_mb = args.output.stat().st_size / 1_048_576
    actual_cps = estimate["chars"] / stats["duration_s"] if stats["duration_s"] else 0
    print(
        f"done: {args.output} — {stats['duration_s']}s, {size_mb:.2f} MB, "
        f"{actual_cps:.1f} chars/s actual"
    )
    if args.keep_blocks_dir:
        print(f"blocks kept in {args.keep_blocks_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
