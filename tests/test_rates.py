"""Per-language speech rates and the duration sanity window.

The regression these guard against is expensive and silent: a single global
sanity window derived from English rejects every correct Japanese block, retries
each one at full credit cost, and then fails the job — while reporting a
plausible-sounding "rendered audio is too long".
"""

from __future__ import annotations

import pytest

import config
from pipeline import _sanity

#: Rates actually measured on v3 renders (chars of script / second of audio).
MEASURED = {"en": 14.1, "ru": 13.2, "ja": 5.6}


@pytest.mark.parametrize("language,rate", MEASURED.items())
def test_measured_rate_passes_its_own_sanity_window(language, rate):
    chars = 1000
    duration = chars / rate
    ok, cps, reason = _sanity(chars, duration, language)
    assert ok, reason
    assert cps == pytest.approx(rate, rel=0.01)


def test_japanese_is_rejected_by_the_english_window():
    """The bug this per-language window exists to prevent."""
    en_rate, en_low, en_high, _ = config.rates_for("en")
    ja_rate, _, _, _ = config.rates_for("ja")
    # Japanese renders well below the English floor...
    assert ja_rate < en_low
    # ...so under an English-derived window a correct Japanese block would fail.
    chars = 1000
    ok, _, _ = _sanity(chars, chars / ja_rate, "en")
    assert not ok
    # But it passes under its own.
    ok, _, reason = _sanity(chars, chars / ja_rate, "ja")
    assert ok, reason


def test_truncated_block_is_caught():
    """Half the audio for the same text: the classic v3 truncation failure."""
    chars = 1000
    honest = chars / MEASURED["en"]
    ok, _, reason = _sanity(chars, honest / 3, "en")
    assert not ok
    assert "too short" in reason and "truncated" in reason


def test_silence_or_looping_is_caught():
    chars = 1000
    honest = chars / MEASURED["en"]
    ok, _, reason = _sanity(chars, honest * 3, "en")
    assert not ok
    assert "too long" in reason


def test_zero_duration_is_caught():
    ok, _, reason = _sanity(500, 0.0, "en")
    assert not ok
    assert "zero duration" in reason


def test_failure_message_names_the_language():
    """An operator reading a failed job must see which window rejected it."""
    ok, _, reason = _sanity(1000, 1000 / 5.6, "en")
    assert not ok
    assert "'en'" in reason


def test_unknown_language_gets_the_wide_window():
    rate, low, high, calibrated = config.rates_for("sw")
    assert calibrated is False
    assert low == config.SANITY_UNKNOWN_MIN_CPS
    assert high == config.SANITY_UNKNOWN_MAX_CPS
    # Wide enough to accept both a Japanese-like and an English-like rate,
    # because for an unmeasured language we genuinely do not know which it is.
    chars = 1000
    assert _sanity(chars, chars / 5.6, "sw")[0]
    assert _sanity(chars, chars / 14.1, "sw")[0]
    # Still catches audio that is obviously broken.
    assert not _sanity(chars, chars / 100, "sw")[0]


@pytest.mark.parametrize("language", ["EN", " ru ", "Ja"])
def test_language_lookup_is_normalised(language):
    _, _, _, calibrated = config.rates_for(language)
    assert calibrated is True


def test_missing_language_does_not_crash():
    for value in (None, "", "   "):
        rate, low, high, calibrated = config.rates_for(value)
        assert calibrated is False
        assert rate > 0 and 0 < low < high


def test_every_calibrated_language_has_a_sane_window():
    for language, rate in config.LANG_CPS.items():
        estimate, low, high, calibrated = config.rates_for(language)
        assert calibrated
        assert estimate == rate
        assert low < rate < high, language
