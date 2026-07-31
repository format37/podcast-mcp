"""``request.yaml`` — the reproducible record of how an episode was made.

Two audiences, one file:

* **You**, in the console: what was this episode, what was it asked to be, and
  what settings produced it.
* **The next agent**, through ``podcast_get_script`` / ``podcast_list``: what has
  already been covered, from what angle, using which sources — so episode twelve
  can build on episode three instead of repeating it.

It deliberately records the *whole* configuration, not just the agent's brief:
voices, model, stability, chunking, mixing and encoding settings as they were at
render time. Config defaults drift, so an episode that sounded right six months
ago is only reproducible if the settings travelled with it.

The companion ``script.txt`` is the same script as readable dialogue — the thing
you actually want to skim or hand to someone, rather than a JSON array.
"""

from __future__ import annotations

from typing import Any

import yaml

import config

#: Fields the agent may supply. Everything else in request.yaml is server-known
#: fact, so a brief can never disagree with what actually happened.
BRIEF_FIELDS = ("topic", "prompt", "angle", "sources", "notes")


class _Dumper(yaml.SafeDumper):
    """Block-style YAML that stays readable when a human opens it."""


def _str_representer(dumper: yaml.Dumper, data: str):
    # Multi-line values (a verbatim user request, research notes) render as
    # literal blocks instead of one unreadable escaped line.
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_Dumper.add_representer(str, _str_representer)


def clean_brief(brief: Any) -> dict[str, Any]:
    """Validate and normalise an agent-supplied brief.

    Lenient on purpose: a malformed brief must never fail a render that would
    otherwise succeed. Unknown keys are kept — an agent that records something
    useful we did not anticipate should not lose it.
    """
    if not isinstance(brief, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in brief.items():
        key = str(key).strip()
        if not key or value is None:
            continue
        if key == "sources":
            if isinstance(value, str):
                out[key] = [value.strip()] if value.strip() else []
            elif isinstance(value, (list, tuple)):
                out[key] = [str(v).strip() for v in value if str(v).strip()]
            continue
        if isinstance(value, (str, int, float, bool)):
            text = str(value).strip() if isinstance(value, str) else value
            if text != "":
                out[key] = text
        elif isinstance(value, (list, dict)):
            out[key] = value
    return out


def build(meta: dict[str, Any], brief: dict[str, Any] | None = None) -> dict[str, Any]:
    """Assemble the request record for an episode from its meta plus the brief."""
    brief = clean_brief(brief)
    # Order the agent's own fields first, then anything extra it invented.
    request: dict[str, Any] = {k: brief[k] for k in BRIEF_FIELDS if k in brief}
    request.update({k: v for k, v in brief.items() if k not in BRIEF_FIELDS})

    record: dict[str, Any] = {
        "episode_id": meta.get("id"),
        "title": meta.get("title"),
        "created": meta.get("created"),
    }
    if meta.get("description"):
        record["description"] = meta["description"]
    record["request"] = request or None
    record["generation"] = {
        "language": meta.get("language"),
        "model_id": meta.get("model_id"),
        "voice_a": meta.get("voice_a"),
        "voice_b": meta.get("voice_b"),
        "stability": config.STABILITY,
        "output_format": config.OUTPUT_FORMAT,
        "block_char_limit": config.BLOCK_CHAR_LIMIT,
        "min_block_chars": config.MIN_BLOCK_CHARS,
        "max_turn_chars": config.MAX_TURN_CHARS,
        "concat_mode": config.CONCAT_MODE,
        "crossfade_ms": config.CROSSFADE_MS if config.CONCAT_MODE == "crossfade" else None,
        "pad_silence_ms": config.PAD_SILENCE_MS if config.CONCAT_MODE == "pad" else None,
        "loudnorm_i": config.LOUDNORM_I,
        "loudnorm_tp": config.LOUDNORM_TP,
        "mp3_bitrate": config.MP3_BITRATE,
        "mp3_channels": config.MP3_CHANNELS,
        "mp3_sample_rate": config.MP3_SAMPLE_RATE,
    }
    record["generation"] = {k: v for k, v in record["generation"].items() if v is not None}

    episode = meta.get("episode") or {}
    record["result"] = {
        "state": meta.get("state"),
        "chars": meta.get("chars"),
        "blocks": meta.get("block_count"),
    }
    if episode:
        record["result"].update(
            {
                "duration_s": episode.get("duration_s"),
                "duration": episode.get("duration_human"),
                "size_mb": episode.get("size_mb"),
                "chars_per_second": episode.get("chars_per_second"),
                "credits_reported": episode.get("credits_reported"),
            }
        )
    record["result"] = {k: v for k, v in record["result"].items() if v is not None}
    return record


def to_yaml(record: dict[str, Any]) -> str:
    header = (
        "# Podcast generation request — the reproducible record for this episode.\n"
        "# 'request' is what the agent was asked for; 'generation' is the exact\n"
        "# configuration that produced the audio; 'result' is what came out.\n"
    )
    body = yaml.dump(
        record,
        Dumper=_Dumper,
        sort_keys=False,
        allow_unicode=True,  # Russian and Japanese titles stay readable
        default_flow_style=False,
        width=100,
    )
    return header + body


def parse_yaml(text: str) -> dict[str, Any]:
    """Read a request.yaml back. Returns {} rather than raising on damaged files."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def script_to_text(script: list[dict[str, Any]], *, title: str = "", meta: dict | None = None) -> str:
    """Render the script as readable dialogue — the download people actually want."""
    lines: list[str] = []
    if title:
        lines += [title, "=" * len(title), ""]
    if meta:
        bits = [
            f"{meta.get('language', '')}".upper(),
            (meta.get("episode") or {}).get("duration_human", ""),
            f"{meta.get('chars', 0)} chars",
            (meta.get("created") or "")[:10],
        ]
        lines += [" · ".join(b for b in bits if b), ""]
    for turn in script:
        speaker = str(turn.get("speaker", "?"))
        text = str(turn.get("text", ""))
        lines.append(f"{speaker}: {text}")
        # A blank line at each preferred chunk boundary mirrors where the audio
        # actually has a seam, so the transcript reads in the same beats.
        if turn.get("block_break_after"):
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"
