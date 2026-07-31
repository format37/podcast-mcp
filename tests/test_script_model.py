"""Contract validation — every rejection must tell the agent how to fix it (spec §8)."""

from __future__ import annotations

import pytest

import config
from script_model import ScriptError, parse_script, total_chars


def ok_script(n: int = 4) -> list[dict]:
    return [
        {"speaker": "HOST_A" if i % 2 == 0 else "HOST_B", "text": f"Line number {i}."}
        for i in range(n)
    ]


def test_valid_script_round_trips():
    turns = parse_script(ok_script())
    assert len(turns) == 4
    assert turns[0].speaker == "HOST_A"
    assert turns[1].speaker == "HOST_B"
    assert turns[0].to_dict() == {"speaker": "HOST_A", "text": "Line number 0."}


def test_block_break_after_is_preserved():
    turns = parse_script([{"speaker": "HOST_A", "text": "Hi.", "block_break_after": True}])
    assert turns[0].block_break_after is True
    assert turns[0].to_dict()["block_break_after"] is True


def test_speaker_is_normalised_and_case_insensitive():
    turns = parse_script([{"speaker": " host_a ", "text": "Hi."}])
    assert turns[0].speaker == "HOST_A"


def test_third_speaker_is_rejected():
    with pytest.raises(ScriptError) as excinfo:
        parse_script([{"speaker": "HOST_C", "text": "Hi."}])
    assert "HOST_A" in str(excinfo.value) and "HOST_B" in str(excinfo.value)


def test_audio_tags_are_allowed_and_counted():
    turns = parse_script([{"speaker": "HOST_A", "text": "[laughs] The curve nobody reads."}])
    assert turns[0].text.startswith("[laughs]")
    assert turns[0].chars == len("[laughs] The curve nobody reads.")


def test_stacked_tags_are_allowed():
    parse_script([{"speaker": "HOST_A", "text": "[whispers][thoughtful] Maybe. [pause] Maybe not."}])


def test_unbalanced_bracket_is_rejected():
    with pytest.raises(ScriptError) as excinfo:
        parse_script([{"speaker": "HOST_A", "text": "[laughs The curve."}])
    assert "bracket" in str(excinfo.value)


def test_tag_only_turn_is_rejected():
    with pytest.raises(ScriptError) as excinfo:
        parse_script([{"speaker": "HOST_A", "text": "[laughs]"}])
    assert "words to speak" in str(excinfo.value)


def test_empty_text_is_rejected():
    with pytest.raises(ScriptError):
        parse_script([{"speaker": "HOST_A", "text": "   "}])


def test_over_long_turn_is_rejected_with_the_fix():
    long_text = "word " * 200
    with pytest.raises(ScriptError) as excinfo:
        parse_script([{"speaker": "HOST_A", "text": long_text}])
    message = str(excinfo.value)
    assert str(config.MAX_TURN_CHARS) in message
    assert "Split it into two" in message


def test_script_over_the_global_cap_is_rejected():
    turn = {"speaker": "HOST_A", "text": "x" * 400}
    count = config.MAX_SCRIPT_CHARS // 400 + 2
    with pytest.raises(ScriptError) as excinfo:
        parse_script([turn] * count)
    assert str(config.MAX_SCRIPT_CHARS) in str(excinfo.value)


def test_all_errors_are_reported_at_once():
    """One round-trip to fix eight problems, not eight."""
    bad = [
        {"speaker": "HOST_C", "text": "one"},
        {"speaker": "HOST_A", "text": ""},
        {"speaker": "HOST_B", "text": "[unclosed"},
        {"speaker": "HOST_A", "text": "fine", "block_break_after": "yes"},
    ]
    with pytest.raises(ScriptError) as excinfo:
        parse_script(bad)
    assert len(excinfo.value.errors) == 4
    assert "4 problems" in excinfo.value.as_text()


def test_error_messages_name_the_turn_index():
    with pytest.raises(ScriptError) as excinfo:
        parse_script([{"speaker": "HOST_A", "text": "ok"}, {"speaker": "NOPE", "text": "bad"}])
    assert "turn 1" in str(excinfo.value)


def test_unknown_field_is_rejected():
    with pytest.raises(ScriptError) as excinfo:
        parse_script([{"speaker": "HOST_A", "text": "Hi.", "voice": "x"}])
    assert "voice" in str(excinfo.value)


def test_object_instead_of_array_names_the_expected_shape():
    with pytest.raises(ScriptError) as excinfo:
        parse_script({"speaker": "HOST_A", "text": "Hi."})
    assert "array" in str(excinfo.value)


def test_empty_script_is_rejected():
    with pytest.raises(ScriptError):
        parse_script([])


def test_non_object_turn_is_rejected():
    with pytest.raises(ScriptError) as excinfo:
        parse_script(["HOST_A: hello"])
    assert "turn 0" in str(excinfo.value)


def test_total_chars_sums_turn_text():
    turns = parse_script(ok_script(3))
    assert total_chars(turns) == sum(len(t.text) for t in turns)
