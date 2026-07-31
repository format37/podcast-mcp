"""Telegram delivery — request shape, limits, and failures that must stay contained."""

from __future__ import annotations

import json

import pytest

import console
import delivery
import settings as settings_mod

# Deliberately NOT shaped like a real Telegram token: the real format is
# <9-10 digits>:<35 chars>, which trips GitHub secret scanning even on an
# obviously fake value. Keep the "digits:secret" shape (mask_token parses it)
# but keep the secret half short and self-describing.
TOKEN = "123456789:TEST-FIXTURE-not-a-real-token"


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


@pytest.fixture(autouse=True)
def configured(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_mod, "PATH", tmp_path / "settings.json")
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_AUTO_SEND"):
        monkeypatch.delenv(key, raising=False)
    settings_mod.save(telegram_bot_token=TOKEN, telegram_chat_id="-100123")
    return tmp_path


@pytest.fixture
def mp3(tmp_path):
    path = tmp_path / "episode.mp3"
    path.write_bytes(b"\xff\xfb" + b"\x00" * 2048)
    return path


META = {
    "id": "20260731-abcdef123456",
    "title": "ROC Curves, Read Properly",
    "description": "Show notes.",
    "episode": {"duration_s": 211.12, "size_mb": 2.42},
}


def capture(monkeypatch, payload=None):
    """Intercept the Bot API call and record what would have been sent."""
    seen = {}

    def fake_post(url, data=None, files=None, timeout=None):
        seen["url"] = url
        seen["data"] = data or {}
        seen["files"] = list((files or {}).keys())
        return FakeResponse(payload or {"ok": True, "result": {
            "message_id": 42, "chat": {"title": "Podcast"}}})

    monkeypatch.setattr(delivery.requests, "post", fake_post)
    return seen


# ── sendAudio ───────────────────────────────────────────────────────────────


def test_uses_sendaudio_not_senddocument(monkeypatch, mp3):
    """sendAudio is what gives the inline player, seek and speed controls."""
    seen = capture(monkeypatch)
    delivery.send_episode(META, mp3)
    assert seen["url"].endswith("/sendAudio")
    assert "audio" in seen["files"]


def test_sends_title_performer_and_duration(monkeypatch, mp3):
    seen = capture(monkeypatch)
    delivery.send_episode(META, mp3)
    assert seen["data"]["title"] == "ROC Curves, Read Properly"
    assert seen["data"]["duration"] == "211"
    assert seen["data"]["performer"]
    assert seen["data"]["chat_id"] == "-100123"


def test_caption_carries_title_and_show_notes(monkeypatch, mp3):
    seen = capture(monkeypatch)
    delivery.send_episode(META, mp3)
    assert "ROC Curves" in seen["data"]["caption"]
    assert "Show notes." in seen["data"]["caption"]


def test_caption_is_truncated_to_telegrams_limit(monkeypatch, mp3):
    seen = capture(monkeypatch)
    delivery.send_episode({**META, "description": "x" * 3000}, mp3)
    assert len(seen["data"]["caption"]) <= delivery.MAX_CAPTION


def test_token_goes_in_the_url_not_the_body(monkeypatch, mp3):
    seen = capture(monkeypatch)
    delivery.send_episode(META, mp3)
    assert TOKEN in seen["url"]
    assert TOKEN not in json.dumps(seen["data"])


def test_success_returns_a_record_for_the_episode(monkeypatch, mp3):
    capture(monkeypatch)
    record = delivery.send_episode(META, mp3)
    assert record["state"] == "sent"
    assert record["message_id"] == 42
    assert record["chat"] == "Podcast"
    assert record["error"] is None


# ── failure modes ───────────────────────────────────────────────────────────


def test_telegram_refusal_surfaces_its_own_wording(monkeypatch, mp3):
    capture(monkeypatch, {"ok": False, "description": "chat not found"})
    with pytest.raises(delivery.DeliveryError) as excinfo:
        delivery.send_episode(META, mp3)
    assert "chat not found" in str(excinfo.value)


def test_oversized_episode_is_refused_before_upload(monkeypatch, tmp_path):
    """Sparse file: real size on stat(), no real bytes on disk."""
    big = tmp_path / "big.mp3"
    with big.open("wb") as handle:
        handle.truncate(delivery.MAX_UPLOAD_BYTES + 1)

    def must_not_upload(*a, **k):
        raise AssertionError("uploaded a file that is over the limit")

    monkeypatch.setattr(delivery.requests, "post", must_not_upload)
    with pytest.raises(delivery.DeliveryError) as excinfo:
        delivery.send_episode(META, big)
    assert "upload limit" in str(excinfo.value)


def test_missing_audio_is_reported_clearly(monkeypatch, tmp_path):
    with pytest.raises(delivery.DeliveryError) as excinfo:
        delivery.send_episode(META, tmp_path / "nope.mp3")
    assert "missing" in str(excinfo.value)


def test_unconfigured_delivery_is_refused(monkeypatch, mp3):
    settings_mod.save(telegram_chat_id="")
    with pytest.raises(delivery.DeliveryError) as excinfo:
        delivery.send_episode(META, mp3)
    assert "chat id" in str(excinfo.value)


def test_network_failure_becomes_a_delivery_error(monkeypatch, mp3):
    def boom(*a, **k):
        raise delivery.requests.RequestException("connection reset")
    monkeypatch.setattr(delivery.requests, "post", boom)
    with pytest.raises(delivery.DeliveryError) as excinfo:
        delivery.send_episode(META, mp3)
    assert "could not reach Telegram" in str(excinfo.value)


def test_non_json_response_is_handled(monkeypatch, mp3):
    monkeypatch.setattr(delivery.requests, "post",
                        lambda *a, **k: FakeResponse(None, status=502))
    with pytest.raises(delivery.DeliveryError) as excinfo:
        delivery.send_episode(META, mp3)
    assert "502" in str(excinfo.value)


def test_failure_and_skip_records_have_the_same_shape():
    for record in (delivery.failure_record("x"), delivery.skipped_record("y")):
        assert set(record) == {"state", "at", "message_id", "chat", "error"}


# ── connection check ────────────────────────────────────────────────────────


def test_check_sends_a_real_message_not_just_getme(monkeypatch):
    """getMe only proves the token; a wrong chat id shows up on an actual send."""
    calls = []

    def fake_post(url, data=None, files=None, timeout=None):
        calls.append(url.rsplit("/", 1)[-1])
        if url.endswith("getMe"):
            return FakeResponse({"ok": True, "result": {"username": "mybot"}})
        return FakeResponse({"ok": True, "result": {"chat": {"title": "Podcast"}}})

    monkeypatch.setattr(delivery.requests, "post", fake_post)
    result = delivery.check()
    assert calls == ["getMe", "sendMessage"]
    assert result["bot"] == "mybot"


def test_check_without_a_chat_id_stops_after_getme(monkeypatch):
    settings_mod.save(telegram_chat_id="")
    calls = []

    def fake_post(url, data=None, files=None, timeout=None):
        calls.append(url.rsplit("/", 1)[-1])
        return FakeResponse({"ok": True, "result": {"username": "mybot"}})

    monkeypatch.setattr(delivery.requests, "post", fake_post)
    result = delivery.check()
    assert calls == ["getMe"]
    assert "No chat id" in result["detail"]


# ── console panel ───────────────────────────────────────────────────────────

EP_META = {**META, "state": "done", "language": "en", "chars": 100, "block_count": 1,
           "created": "2026-07-31T10:00:00Z",
           "episode": {"duration_s": 211.12, "duration_human": "3m 31s", "size_mb": 2.4},
           "blocks": [], "progress": {"message": "done"}}


def episode_html(meta, ready=True):
    import brief
    return console.render_episode(meta, [], brief.build(meta, None),
                                  base="/podcast/console",
                                  available={"episode.mp3"}, telegram_ready=ready)


def test_episode_page_shows_delivery_state_and_send_button():
    html = episode_html({**EP_META, "delivery": {
        "state": "sent", "at": "2026-07-31T10:05:00Z", "chat": "Podcast"}})
    assert "SENT" in html and "Podcast" in html
    assert "SEND AGAIN" in html


def test_unsent_episode_offers_to_send():
    html = episode_html(EP_META)
    assert "SEND TO TELEGRAM" in html


def test_send_button_is_replaced_by_a_pointer_when_unconfigured():
    html = episode_html(EP_META, ready=False)
    assert "SEND TO TELEGRAM" not in html
    assert "/podcast/console/settings" in html


def test_failed_delivery_shows_the_reason():
    html = episode_html({**EP_META, "delivery": {
        "state": "failed", "error": "chat not found"}})
    assert "FAILED" in html and "chat not found" in html


def test_delivery_error_text_is_escaped():
    html = episode_html({**EP_META, "delivery": {
        "state": "failed", "error": "<script>alert(1)</script>"}})
    assert "<script>alert" not in html


def test_unfinished_episode_has_no_delivery_panel():
    html = episode_html({**EP_META, "state": "rendering", "episode": None})
    assert "SEND TO TELEGRAM" not in html
