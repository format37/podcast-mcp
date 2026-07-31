"""Pack turns into render blocks (spec §5).

The endpoint takes at most 2,000 characters summed across a request's inputs, so
a 25-minute episode is a dozen-plus separate generations glued together. Every
join is an audible risk — and because ``eleven_v3`` does **not** support request
stitching (``previous_request_ids`` exists only on the plain TTS endpoint, and
is documented as unavailable for v3), there is no way to condition block N+1 on
block N. Seam quality is therefore decided entirely here, by *where* we cut.

Hence the ordering of preferences:

1. Never split a turn — a cut mid-turn is a cut mid-thought.
2. Cut where the writer asked (``block_break_after``), because they put a
   topic-transition line there and a tempo change hides a seam.
3. Otherwise cut as late as fits, so blocks stay long and seams stay few.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import config
from script_model import Turn


@dataclass(slots=True)
class Block:
    index: int
    turns: list[Turn] = field(default_factory=list)
    #: Why this block ended — surfaced in job metadata so a bad seam can be
    #: traced back to the decision that made it.
    boundary: str = "end"

    @property
    def chars(self) -> int:
        return sum(t.chars for t in self.turns)

    def inputs(self, voice_a: str, voice_b: str) -> list[dict[str, str]]:
        """The request body's ``inputs`` array for this block."""
        return [
            {"text": t.text, "voice_id": voice_a if t.speaker == "HOST_A" else voice_b}
            for t in self.turns
        ]


def chunk(
    turns: list[Turn],
    limit: int | None = None,
    min_block_chars: int | None = None,
) -> list[Block]:
    """Greedily pack ``turns`` into blocks of at most ``limit`` characters.

    ``min_block_chars`` guards the marker preference: a writer who marks every
    turn would otherwise get one API call per turn — the most seams, the most
    latency, and the worst prosody possible. Below that floor a marker is
    treated as advisory and ignored.
    """
    limit = config.BLOCK_CHAR_LIMIT if limit is None else limit
    floor = config.MIN_BLOCK_CHARS if min_block_chars is None else min_block_chars
    # A floor at or above the limit would suppress every marker; clamp so the
    # marker preference degrades gracefully instead of vanishing.
    floor = min(floor, limit)

    blocks: list[Block] = []
    current = Block(index=0)

    def close(boundary: str) -> None:
        nonlocal current
        current.boundary = boundary
        blocks.append(current)
        current = Block(index=len(blocks))

    for turn in turns:
        # Rule 1+3: this turn does not fit, so the block ends before it. A turn
        # longer than the whole limit is rejected upstream by parse_script, so
        # the block we close here is never empty.
        if current.turns and current.chars + turn.chars > limit:
            close("size")

        current.turns.append(turn)

        # Rule 2: the writer's preferred boundary, honoured once the block has
        # enough substance to stand on its own.
        if turn.block_break_after and current.chars >= floor:
            close("marker")

    if current.turns:
        blocks.append(current)
    elif blocks:
        # The last turn carried a marker, so the trailing block is empty.
        blocks[-1].boundary = "end"

    return blocks


def summarize(blocks: list[Block]) -> list[dict[str, object]]:
    """Compact per-block description for dry runs and job metadata."""
    return [
        {
            "index": b.index,
            "turns": len(b.turns),
            "chars": b.chars,
            "boundary": b.boundary,
            "first_line": b.turns[0].text[:60] if b.turns else "",
        }
        for b in blocks
    ]
