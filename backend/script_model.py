"""The script-format contract (spec §4) — the only interface between skill and server.

Validation is deliberately strict and deliberately *chatty*: every message names
the offending turn index and states the fix, because the agent that wrote the
script is expected to self-correct from the error text alone without a human in
the loop (spec §8). Errors are collected, not raised on the first problem — a
script with eight bad turns should take one round-trip to fix, not eight.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import config


class ScriptError(ValueError):
    """A script that violates the contract. ``errors`` holds every problem found."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))

    def as_text(self) -> str:
        if len(self.errors) == 1:
            return self.errors[0]
        lines = "\n".join(f"  {i}. {e}" for i, e in enumerate(self.errors, 1))
        return f"{len(self.errors)} problems with the script:\n{lines}"


@dataclass(slots=True)
class Turn:
    speaker: str
    text: str
    block_break_after: bool = False

    @property
    def chars(self) -> int:
        return len(self.text)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"speaker": self.speaker, "text": self.text}
        if self.block_break_after:
            d["block_break_after"] = True
        return d


_KNOWN_KEYS = {"speaker", "text", "block_break_after"}
#: An audio tag is any [bracketed] span. We do not police the vocabulary — v3
#: accepts free-form direction ("[thoughtful]", "[leaves rustling]") — only that
#: the brackets are balanced, since an unclosed one gets *spoken aloud*.
_TAG_RE = re.compile(r"\[([^\[\]]*)\]")


def _tag_problems(text: str) -> str | None:
    stripped = _TAG_RE.sub("", text)
    if "[" in stripped or "]" in stripped:
        return (
            "has an unbalanced square bracket — audio tags must be closed, e.g. "
            "'[laughs]'. An unclosed bracket is read out loud by the model"
        )
    if not _TAG_RE.sub("", text).strip():
        return "is nothing but audio tags; every turn needs words to speak"
    return None


def parse_script(raw: Any) -> list[Turn]:
    """Validate a raw script payload and return typed turns, or raise :class:`ScriptError`."""
    errors: list[str] = []

    if isinstance(raw, dict):  # a common near-miss worth naming precisely
        raise ScriptError([
            "script must be a JSON array of turn objects, not an object. "
            'Expected: [{"speaker": "HOST_A", "text": "..."}, ...]'
        ])
    if not isinstance(raw, (list, tuple)):
        raise ScriptError([
            f"script must be a JSON array of turn objects, got {type(raw).__name__}. "
            'Expected: [{"speaker": "HOST_A", "text": "..."}, ...]'
        ])
    if not raw:
        raise ScriptError(["script is empty — it needs at least one turn."])

    turns: list[Turn] = []
    total = 0
    for i, item in enumerate(raw):
        where = f"turn {i}"
        if not isinstance(item, dict):
            errors.append(
                f'{where} is a {type(item).__name__}, not an object. Each turn must be '
                '{"speaker": "HOST_A"|"HOST_B", "text": "..."}.'
            )
            continue

        unknown = set(item) - _KNOWN_KEYS
        if unknown:
            errors.append(
                f"{where} has unsupported field(s) {sorted(unknown)}; "
                f"only {sorted(_KNOWN_KEYS)} are allowed."
            )

        speaker = item.get("speaker")
        if not isinstance(speaker, str) or speaker.strip().upper() not in config.SPEAKERS:
            errors.append(
                f"{where} has speaker={speaker!r}; v1 supports exactly two speakers, "
                f"{' and '.join(config.SPEAKERS)}."
            )
            speaker = None
        else:
            speaker = speaker.strip().upper()

        text = item.get("text")
        if not isinstance(text, str):
            errors.append(f"{where} has text={type(text).__name__}, expected a string.")
            text = None
        else:
            text = text.strip()
            if not text:
                errors.append(f"{where} has empty text; drop the turn or give it words.")
                text = None
            elif len(text) > config.MAX_TURN_CHARS:
                errors.append(
                    f"{where} is {len(text)} chars, over the {config.MAX_TURN_CHARS}-char "
                    "per-turn limit. Split it into two consecutive turns by the same "
                    "speaker — short turns are what make the dialogue sound natural."
                )
                text = None
            else:
                problem = _tag_problems(text)
                if problem:
                    errors.append(f"{where} {problem}.")
                    text = None

        brk = item.get("block_break_after", False)
        if not isinstance(brk, bool):
            errors.append(
                f"{where} has block_break_after={brk!r}; it must be true or false."
            )
            brk = False

        if speaker and text:
            total += len(text)
            turns.append(Turn(speaker=speaker, text=text, block_break_after=brk))

    if total > config.MAX_SCRIPT_CHARS:
        errors.append(
            f"script totals {total} chars, over the {config.MAX_SCRIPT_CHARS}-char cap "
            f"(~{config.MAX_SCRIPT_CHARS // 1000} minutes of audio). Split the topic "
            "across two episodes."
        )

    # A single turn longer than a whole block can never be rendered: the chunker
    # is forbidden from splitting turns, so this is a hard stop rather than a
    # surprise mid-render. Only reachable with a hand-lowered BLOCK_CHAR_LIMIT.
    for i, t in enumerate(turns):
        if t.chars > config.BLOCK_CHAR_LIMIT:
            errors.append(
                f"turn {i} is {t.chars} chars, longer than the {config.BLOCK_CHAR_LIMIT}-char "
                "render block, and turns are never split across blocks. Shorten it."
            )

    if errors:
        raise ScriptError(errors)
    return turns


def total_chars(turns: list[Turn]) -> int:
    return sum(t.chars for t in turns)
