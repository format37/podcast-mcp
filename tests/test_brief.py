"""request.yaml — the reproducible record handed to the console and to future agents."""

from __future__ import annotations

import brief
import config


def meta(**over):
    base = {
        "id": "20260731-abcdef123456",
        "title": "ROC Curves, Read Properly",
        "created": "2026-07-31T10:00:00Z",
        "description": "Show notes.",
        "language": "en",
        "model_id": "eleven_v3",
        "voice_a": "voice-a",
        "voice_b": "voice-b",
        "chars": 2986,
        "block_count": 3,
        "state": "done",
        "episode": {"duration_s": 211.12, "duration_human": "3m 31s", "size_mb": 2.42,
                    "chars_per_second": 14.1, "credits_reported": 1642},
    }
    base.update(over)
    return base


def test_record_has_the_three_sections():
    record = brief.build(meta(), {"topic": "ROC curves"})
    assert set(record) >= {"episode_id", "title", "request", "generation", "result"}


def test_generation_captures_the_settings_that_produced_the_audio():
    """Config defaults drift; an episode is only reproducible if they travelled with it."""
    generation = brief.build(meta(), None)["generation"]
    assert generation["model_id"] == "eleven_v3"
    assert generation["voice_a"] == "voice-a"
    assert generation["block_char_limit"] == config.BLOCK_CHAR_LIMIT
    assert generation["concat_mode"] == config.CONCAT_MODE
    assert generation["mp3_bitrate"] == config.MP3_BITRATE


def test_only_the_active_concat_mode_is_recorded():
    generation = brief.build(meta(), None)["generation"]
    if config.CONCAT_MODE == "crossfade":
        assert "crossfade_ms" in generation and "pad_silence_ms" not in generation
    else:
        assert "pad_silence_ms" in generation and "crossfade_ms" not in generation


def test_result_reflects_the_finished_episode():
    result = brief.build(meta(), None)["result"]
    assert result["state"] == "done"
    assert result["duration_s"] == 211.12
    assert result["chars"] == 2986


def test_yaml_round_trips():
    record = brief.build(meta(), {"topic": "ROC", "sources": ["https://example.com"]})
    text = brief.to_yaml(record)
    back = brief.parse_yaml(text)
    assert back["request"]["topic"] == "ROC"
    assert back["request"]["sources"] == ["https://example.com"]
    assert back["generation"]["model_id"] == "eleven_v3"


def test_yaml_keeps_cyrillic_and_japanese_readable():
    """allow_unicode: a Russian title escaped to \\uXXXX is unusable in the console."""
    record = brief.build(meta(title="Почему модели галлюцинируют"), {"topic": "日本語のトピック"})
    text = brief.to_yaml(record)
    assert "Почему модели галлюцинируют" in text
    assert "日本語のトピック" in text
    assert "\\u" not in text


def test_multiline_values_use_literal_blocks():
    record = brief.build(meta(), {"notes": "line one\nline two"})
    text = brief.to_yaml(record)
    assert "|-" in text or "|\n" in text
    assert brief.parse_yaml(text)["request"]["notes"] == "line one\nline two"


def test_yaml_survives_colons_and_quotes_in_agent_text():
    tricky = 'Topic: "quoted", then #hash and a: colon'
    record = brief.build(meta(), {"prompt": tricky})
    assert brief.parse_yaml(brief.to_yaml(record))["request"]["prompt"] == tricky


def test_brief_field_order_is_stable():
    record = brief.build(meta(), {"notes": "n", "topic": "t", "angle": "a"})
    assert list(record["request"]) == ["topic", "angle", "notes"]


def test_unknown_brief_keys_are_kept():
    """An agent recording something we didn't anticipate should not lose it."""
    record = brief.build(meta(), {"topic": "t", "series": "metrics, part 2"})
    assert record["request"]["series"] == "metrics, part 2"


def test_a_single_source_string_becomes_a_list():
    assert brief.clean_brief({"sources": "https://a.example"})["sources"] == ["https://a.example"]


def test_malformed_brief_never_breaks_a_render():
    for junk in (None, "a string", 42, [], {"topic": None}, {"": "x"}):
        assert isinstance(brief.clean_brief(junk), dict)
    assert brief.build(meta(), "not a dict")["request"] is None


def test_parse_yaml_tolerates_a_damaged_file():
    assert brief.parse_yaml("{{{ not yaml") == {}
    assert brief.parse_yaml("- just\n- a list") == {}


def test_missing_episode_still_produces_a_record():
    record = brief.build(meta(episode=None, state="rendering"), {"topic": "t"})
    assert record["result"]["state"] == "rendering"
    assert "duration_s" not in record["result"]


# ── transcript ──────────────────────────────────────────────────────────────

SCRIPT = [
    {"speaker": "HOST_A", "text": "[excited] First line."},
    {"speaker": "HOST_B", "text": "Second line.", "block_break_after": True},
    {"speaker": "HOST_A", "text": "Third line."},
]


def test_transcript_is_readable_dialogue():
    text = brief.script_to_text(SCRIPT, title="Ep", meta=meta())
    assert "HOST_A: [excited] First line." in text
    assert "HOST_B: Second line." in text
    assert text.startswith("Ep\n==")


def test_transcript_blank_line_marks_the_audio_seam():
    text = brief.script_to_text(SCRIPT)
    lines = text.splitlines()
    assert lines[lines.index("HOST_B: Second line.") + 1] == ""


def test_transcript_handles_an_empty_script():
    assert brief.script_to_text([]) == "\n"
