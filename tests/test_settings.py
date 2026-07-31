"""Telegram delivery settings — persistence, and never leaking the bot token."""

from __future__ import annotations

import json
import stat

import pytest

import console
import settings as settings_mod

TOKEN = "123456789:AAHrealSecretTokenValue0123456789xyz"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Point the store at a temp file and clear the env for every test."""
    monkeypatch.setattr(settings_mod, "PATH", tmp_path / "settings.json")
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_AUTO_SEND"):
        monkeypatch.delenv(key, raising=False)
    return tmp_path


# ── load / save ─────────────────────────────────────────────────────────────


def test_defaults_when_nothing_is_configured():
    values = settings_mod.load()
    assert values == {"telegram_bot_token": "", "telegram_chat_id": "",
                      "telegram_auto_send": False}


def test_environment_seeds_the_values(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100123")
    monkeypatch.setenv("TELEGRAM_AUTO_SEND", "true")
    values = settings_mod.load()
    assert values["telegram_bot_token"] == TOKEN
    assert values["telegram_chat_id"] == "-100123"
    assert values["telegram_auto_send"] is True


def test_saved_values_win_over_the_environment(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100999")
    settings_mod.save(telegram_chat_id="-100123")
    assert settings_mod.load()["telegram_chat_id"] == "-100123"


def test_save_round_trips():
    settings_mod.save(telegram_bot_token=TOKEN, telegram_chat_id="-100123",
                      telegram_auto_send=True)
    values = settings_mod.load()
    assert values["telegram_bot_token"] == TOKEN
    assert values["telegram_auto_send"] is True


def test_partial_save_keeps_other_fields():
    settings_mod.save(telegram_bot_token=TOKEN, telegram_chat_id="-100123")
    settings_mod.save(telegram_auto_send=True)
    values = settings_mod.load()
    assert values["telegram_bot_token"] == TOKEN
    assert values["telegram_chat_id"] == "-100123"


def test_the_mask_sentinel_never_overwrites_a_stored_token():
    """The form posts the mask back when the operator did not retype it."""
    settings_mod.save(telegram_bot_token=TOKEN)
    settings_mod.save(telegram_bot_token=settings_mod.MASK_SENTINEL,
                      telegram_chat_id="-100123")
    assert settings_mod.load()["telegram_bot_token"] == TOKEN


def test_a_token_can_still_be_cleared():
    settings_mod.save(telegram_bot_token=TOKEN)
    settings_mod.save(telegram_bot_token="")
    assert settings_mod.load()["telegram_bot_token"] == ""


def test_unknown_keys_are_ignored():
    settings_mod.save(nonsense="x", telegram_chat_id="-1")
    assert "nonsense" not in settings_mod.load()


def test_settings_file_is_not_world_readable():
    """It holds a live bot token."""
    settings_mod.save(telegram_bot_token=TOKEN)
    mode = settings_mod.PATH.stat().st_mode
    assert not mode & stat.S_IROTH
    assert not mode & stat.S_IRGRP


def test_a_damaged_settings_file_falls_back_instead_of_crashing():
    settings_mod.PATH.write_text("{{{ not json", encoding="utf-8")
    assert settings_mod.load()["telegram_chat_id"] == ""


def test_auto_send_is_coerced_to_a_bool():
    settings_mod.PATH.write_text(json.dumps({"telegram_auto_send": "yes"}), encoding="utf-8")
    assert settings_mod.load()["telegram_auto_send"] is True


# ── masking ─────────────────────────────────────────────────────────────────


def test_mask_hides_the_secret_half_but_keeps_the_bot_id():
    masked = settings_mod.mask_token(TOKEN)
    assert masked.startswith("123456789:")
    assert "AAHrealSecretTokenValue" not in masked
    assert masked.endswith("9xyz")


def test_mask_of_an_empty_token_is_empty():
    assert settings_mod.mask_token("") == ""


def test_mask_handles_a_malformed_token():
    masked = settings_mod.mask_token("garbage-without-a-colon")
    assert "garbage" not in masked


def test_public_view_never_carries_the_raw_token():
    settings_mod.save(telegram_bot_token=TOKEN, telegram_chat_id="-100123")
    view = settings_mod.public_view()
    assert TOKEN not in json.dumps(view)
    assert view["telegram_bot_token_set"] is True
    assert view["configured"] is True


def test_delivery_ready_requires_both_halves():
    assert settings_mod.delivery_ready()[0] is False
    settings_mod.save(telegram_bot_token=TOKEN)
    assert settings_mod.delivery_ready()[0] is False
    settings_mod.save(telegram_chat_id="-100123")
    assert settings_mod.delivery_ready()[0] is True


# ── the settings page ───────────────────────────────────────────────────────


def test_settings_page_never_renders_the_raw_token():
    """The single most important property of this page."""
    settings_mod.save(telegram_bot_token=TOKEN, telegram_chat_id="-100123")
    html = console.render_settings(settings_mod.public_view(), base="/podcast/console")
    assert TOKEN not in html
    assert "AAHrealSecretTokenValue" not in html
    # The field is pre-filled with the sentinel so an unchanged save is a no-op.
    assert settings_mod.MASK_SENTINEL in html


def test_settings_page_shows_the_chat_id_and_toggle_state():
    settings_mod.save(telegram_chat_id="-100123", telegram_auto_send=True)
    html = console.render_settings(settings_mod.public_view(), base="/podcast/console")
    assert 'value="-100123"' in html
    assert 'id="auto" name="telegram_auto_send" checked' in html


def test_toggle_renders_unchecked_when_off():
    settings_mod.save(telegram_auto_send=False)
    html = console.render_settings(settings_mod.public_view(), base="/podcast/console")
    assert "checked" not in html


def test_settings_page_posts_to_the_token_prefixed_url():
    html = console.render_settings(settings_mod.public_view(), base="/podcast/SECRET/console")
    assert 'action="/podcast/SECRET/console/settings"' in html
    assert 'href="/podcast/SECRET/console"' in html


def test_settings_page_reports_save_test_and_error():
    view = settings_mod.public_view()
    assert "Settings saved" in console.render_settings(view, base="/x", saved=True)
    assert "Sent a test" in console.render_settings(view, base="/x", tested="Sent a test message")
    html = console.render_settings(view, base="/x", error="chat not found")
    assert "chat not found" in html and "notice-bad" in html


def test_error_text_from_telegram_is_escaped():
    html = console.render_settings(settings_mod.public_view(), base="/x",
                                   error='<script>alert(1)</script>')
    assert "<script>alert" not in html


def test_masthead_links_to_settings():
    html = console.render_index([], base="/podcast/console")
    assert 'href="/podcast/console/settings"' in html
