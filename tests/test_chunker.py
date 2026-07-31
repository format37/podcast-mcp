"""Chunker behaviour — the seam decisions, since v3 gives us no stitching to fall back on."""

from __future__ import annotations

import pytest

import chunker
import config
from script_model import Turn


def turn(chars: int, speaker: str = "HOST_A", brk: bool = False) -> Turn:
    return Turn(speaker=speaker, text="x" * chars, block_break_after=brk)


def test_short_script_is_one_block():
    blocks = chunker.chunk([turn(100), turn(100)], limit=1800)
    assert len(blocks) == 1
    assert blocks[0].chars == 200
    assert blocks[0].boundary == "end"


def test_never_exceeds_the_limit():
    blocks = chunker.chunk([turn(400) for _ in range(10)], limit=1000)
    assert [b.chars for b in blocks] == [800, 800, 800, 800, 800]
    assert all(b.chars <= 1000 for b in blocks)


def test_turns_are_never_split():
    turns = [turn(300) for _ in range(7)]
    blocks = chunker.chunk(turns, limit=1000)
    assert sum(len(b.turns) for b in blocks) == 7
    # Every original turn survives intact and in order.
    assert [t.text for b in blocks for t in b.turns] == [t.text for t in turns]


def test_block_break_marker_is_honoured():
    turns = [turn(400), turn(400, brk=True), turn(400), turn(400)]
    blocks = chunker.chunk(turns, limit=1800, min_block_chars=600)
    assert len(blocks) == 2
    assert blocks[0].chars == 800
    assert blocks[0].boundary == "marker"


def test_marker_below_the_floor_is_ignored():
    """A marker on a tiny block would buy an extra seam for nothing."""
    turns = [turn(100, brk=True), turn(400), turn(400)]
    blocks = chunker.chunk(turns, limit=1800, min_block_chars=600)
    assert len(blocks) == 1
    assert blocks[0].chars == 900


def test_marker_fallback_when_the_marked_boundary_would_overflow():
    """Spec §5.2: an unreachable marker degrades to the last boundary that fits."""
    turns = [turn(400), turn(400), turn(400), turn(400, brk=True), turn(400)]
    blocks = chunker.chunk(turns, limit=1000, min_block_chars=600)
    assert all(b.chars <= 1000 for b in blocks)
    # The size-forced cuts land before the marker is ever reachable.
    assert blocks[0].boundary == "size"
    assert [b.chars for b in blocks] == [800, 800, 400]


def test_marker_on_the_final_turn_leaves_no_empty_block():
    blocks = chunker.chunk([turn(400), turn(400, brk=True)], limit=1800, min_block_chars=600)
    assert len(blocks) == 1
    assert blocks[0].turns
    assert blocks[0].boundary == "end"


def test_every_turn_marked_still_respects_the_floor():
    turns = [turn(200, brk=True) for _ in range(9)]
    blocks = chunker.chunk(turns, limit=1800, min_block_chars=600)
    # 600-char floor => cuts at 600, not nine one-turn blocks.
    assert [b.chars for b in blocks] == [600, 600, 600]
    # The trailing block reports "end": no seam follows it, so no seam decision
    # was made there, even though its last turn carried a marker.
    assert [b.boundary for b in blocks] == ["marker", "marker", "end"]


def test_floor_above_limit_does_not_suppress_markers_entirely():
    turns = [turn(400, brk=True) for _ in range(4)]
    blocks = chunker.chunk(turns, limit=800, min_block_chars=5000)
    assert all(b.chars <= 800 for b in blocks)
    assert len(blocks) == 2


def test_block_indexes_are_contiguous():
    blocks = chunker.chunk([turn(300) for _ in range(11)], limit=900)
    assert [b.index for b in blocks] == list(range(len(blocks)))


def test_inputs_map_speakers_to_voices():
    blocks = chunker.chunk([turn(10, "HOST_A"), turn(10, "HOST_B")], limit=1800)
    inputs = blocks[0].inputs("voice-a", "voice-b")
    assert [i["voice_id"] for i in inputs] == ["voice-a", "voice-b"]
    assert all(set(i) == {"text", "voice_id"} for i in inputs)


def test_empty_script_yields_no_blocks():
    assert chunker.chunk([], limit=1800) == []


def test_default_limit_respects_the_api_ceiling():
    """The endpoint takes 2,000 chars per request; the default must stay under it."""
    assert config.BLOCK_CHAR_LIMIT <= config.API_CHAR_CEILING
    blocks = chunker.chunk([turn(config.MAX_TURN_CHARS) for _ in range(20)])
    assert all(b.chars <= config.API_CHAR_CEILING for b in blocks)


def test_summarize_reports_every_block():
    blocks = chunker.chunk([turn(400) for _ in range(5)], limit=1000)
    summary = chunker.summarize(blocks)
    assert len(summary) == len(blocks)
    assert {"index", "turns", "chars", "boundary", "first_line"} == set(summary[0])


@pytest.mark.parametrize("limit", [500, 800, 1200, 1800, 2000])
def test_packing_is_tight_for_any_limit(limit):
    """Greedy packing: no block could have taken the next turn as well."""
    turns = [turn(137) for _ in range(30)]
    blocks = chunker.chunk(turns, limit=limit, min_block_chars=0)
    for current, following in zip(blocks, blocks[1:]):
        assert current.chars + following.turns[0].chars > limit
