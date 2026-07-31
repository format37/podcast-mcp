"""Server-rendered private console — browse, play, download and delete episodes.

Plain HTML out of the same process that owns the episodes. No build step, no
second container, no JSON API in between: the console cannot drift out of sync
with the data because it *is* the data. Client-side JavaScript is one thing only
— the ~15-line theme toggle, lifted from blog-site — so the page renders fully
with JS disabled.

Visually it is the third panel of the same instrument as blog-site and
portfolio-site: their token block verbatim, their day/night mechanism, and their
shared ``portfolio-theme`` localStorage key, so switching to night on the blog
switches it here too (same origin on the VPS).
"""

from __future__ import annotations

import html
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import brief as brief_mod
import config
import episodes
from settings import MASK_SENTINEL

logger = logging.getLogger(__name__)

_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
           "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


def _asset_version() -> str:
    """Cache-buster derived from the newest mtime among the served assets.

    Assets are served with a week-long cache (requested on every page load,
    unchanged between deploys), which without this would mean a style or icon
    fix does not reach a browser for seven days — and, worse, that new HTML
    renders against stale CSS. Covering the whole directory rather than just
    the stylesheet means changing only the favicon still bumps the version.
    Fonts are excluded: they are content-stable and would only add churn.
    """
    static = Path(__file__).resolve().parent / "static"
    try:
        newest = max(
            (p.stat().st_mtime for p in static.iterdir() if p.is_file()), default=0.0
        )
        return str(int(newest))
    except OSError:
        return "0"


ASSET_V = _asset_version()

#: Files an episode page offers for download, in the order they are shown.
DOWNLOADS = (
    ("episode.mp3", "MP3", "the finished audio"),
    ("script.txt", "TRANSCRIPT", "the dialogue as readable text"),
    ("request.yaml", "REQUEST.YAML", "the full generation configuration"),
    ("script.json", "SCRIPT.JSON", "the script as submitted"),
)

# The pre-paint script, verbatim in spirit from blog-site/src/layouts/Base.astro:
# it must be the first thing in <head>, before any stylesheet, or the wrong
# theme paints for one frame. Light is the boot default, matching the siblings.
_PREPAINT = """
try { document.documentElement.dataset.theme =
        localStorage.getItem('portfolio-theme') || 'light'; }
catch (e) { document.documentElement.dataset.theme = 'light'; }
"""

# Selection is progressive enhancement. Without JS the checkboxes are still real
# form inputs and "delete selected" still submits — only the select-all box, the
# live count and the confirm step need scripting.
_SELECT_JS = """
(function () {
  var form = document.getElementById('bulk');
  if (!form) return;
  var all = document.getElementById('pick-all');
  var btn = document.getElementById('bulk-delete');
  var count = document.getElementById('bulk-count');
  function picks() {
    return Array.prototype.slice.call(form.querySelectorAll('.pick:not(:disabled)'));
  }
  function sync() {
    var boxes = picks();
    var on = boxes.filter(function (b) { return b.checked; });
    count.textContent = on.length ? on.length + ' SELECTED' : '';
    btn.disabled = on.length === 0;
    if (all) {
      all.checked = on.length > 0 && on.length === boxes.length;
      all.indeterminate = on.length > 0 && on.length < boxes.length;
    }
  }
  if (all) {
    all.addEventListener('change', function () {
      picks().forEach(function (b) { b.checked = all.checked; });
      sync();
    });
  }
  form.addEventListener('change', function (e) {
    if (e.target.classList.contains('pick')) sync();
  });
  form.addEventListener('submit', function (e) {
    var n = picks().filter(function (b) { return b.checked; }).length;
    if (!n || !confirm('Delete ' + n + ' episode' + (n > 1 ? 's' : '') +
        '? The audio, script and request.yaml are removed permanently.')) {
      e.preventDefault();
    }
  });
  sync();
})();
"""

_TOGGLE_JS = """
(function () {
  var btn = document.getElementById('theme-toggle');
  if (!btn) return;
  var CANVAS = { dark: '#0a0a0c', light: '#eceae5' };
  function cur() { return document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light'; }
  function paint() {
    var dark = cur() === 'dark';
    btn.textContent = dark ? '\\u263e NIGHT' : '\\u2600 DAY';
    btn.setAttribute('aria-label', dark ? 'Switch to day theme' : 'Switch to night theme');
    var m = document.querySelector('meta[name="theme-color"]');
    if (m) m.setAttribute('content', CANVAS[cur()]);
  }
  btn.addEventListener('click', function () {
    var next = cur() === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('portfolio-theme', next); } catch (e) {}
    paint();
  });
  paint();
})();
"""


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _fmt_date(iso: str | None, *, long: bool = False) -> str:
    if not iso:
        return ""
    try:
        stamp = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return esc(str(iso)[:10])
    month = _MONTHS[stamp.month - 1]
    return f"{month} {stamp.day}, {stamp.year}" if long else f"{month} {stamp.day}"


def _group_key(iso: str | None) -> str:
    if not iso:
        return "UNDATED"
    try:
        stamp = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return "UNDATED"
    return f"{_MONTHS[stamp.month - 1]} {stamp.year}"


def _hms(seconds: Any) -> str:
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        return ""
    return f"{total // 60}m {total % 60:02d}s"


def _shell(*, title: str, base: str, body: str, read: bool = False, select: bool = False) -> str:
    """The page frame: head, masthead, content, footer."""
    wrap = "wrap wrap-read" if read else "wrap"
    scripts = _TOGGLE_JS + (_SELECT_JS if select else "")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<script>{_PREPAINT}</script>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<meta name="robots" content="noindex, nofollow">
<meta name="color-scheme" content="light dark">
<meta name="theme-color" content="#eceae5">
<link rel="icon" type="image/svg+xml" href="{base}/assets/favicon.svg?v={ASSET_V}">
<link rel="alternate icon" type="image/png" href="{base}/assets/favicon-32.png?v={ASSET_V}">
<link rel="apple-touch-icon" href="{base}/assets/apple-touch-icon.png?v={ASSET_V}">
<link rel="preload" href="{base}/assets/fonts/jetbrains-mono-400.woff2" as="font" type="font/woff2" crossorigin>
<link rel="preload" href="{base}/assets/fonts/inter-400.woff2" as="font" type="font/woff2" crossorigin>
<link rel="stylesheet" href="{base}/assets/console.css?v={ASSET_V}">
</head>
<body>
<div class="{wrap}">
<header class="mast">
  <a class="brand" href="{base}">{esc(config.SHOW_TITLE)} <span class="dim">// console</span></a>
  <nav class="mast-right">
    <a class="plink" href="{base}/settings">SETTINGS</a>
    <button class="toggle" id="theme-toggle" aria-label="Switch theme">&#9728; DAY</button>
  </nav>
</header>
{body}
<footer class="foot">
  <span>PODCAST <span class="sep">//</span> CONSOLE</span>
  <span>PRIVATE &#183; NOT INDEXED</span>
</footer>
</div>
<script>{scripts}</script>
</body>
</html>"""


# --------------------------------------------------------------------------
# Index
# --------------------------------------------------------------------------


def _stat(label: str, value: str, unit: str = "", extra: str = "") -> str:
    unit_html = f' <span class="unit">{esc(unit)}</span>' if unit else ""
    return (
        f'<div class="stat"><span class="stat-k">{esc(label)}</span>'
        f'<span class="stat-v">{esc(value)}{unit_html}</span>{extra}</div>'
    )


#: States whose files the render worker may still be writing — deleting one
#: would pull the directory out from under it, so they cannot be selected.
BUSY_STATES = ("queued", "validated", "rendering", "mixing")


def _result_note(deleted: int, failed: int) -> str:
    if not deleted and not failed:
        return ""
    bits = []
    if deleted:
        bits.append(f"Deleted {deleted} episode{'s' if deleted != 1 else ''}.")
    if failed:
        bits.append(
            f"{failed} could not be deleted (still rendering, or already gone)."
        )
    return f'<p class="note notice">{esc(" ".join(bits))}</p>'


def render_index(
    items: list[dict[str, Any]],
    *,
    base: str,
    quota: dict | None = None,
    deleted: int = 0,
    failed: int = 0,
) -> str:
    done = [e for e in items if e.get("state") == "done"]
    total_s = sum(float(e.get("duration_s") or 0) for e in done)
    total_mb = sum(float(e.get("size_bytes") or 0) for e in done) / 1_048_576

    stats = [
        _stat("Episodes", str(len(items))),
        _stat("Total audio", _hms(total_s) or "0m 00s"),
        _stat("On disk", f"{total_mb:.1f}", "MB"),
    ]
    if quota and isinstance(quota.get("characters_used"), int):
        used = quota["characters_used"]
        limit = quota.get("characters_limit") or 0
        pct = (used / limit * 100) if limit else 0
        bar = f'<div class="bar"><i style="width:{min(100, max(0, pct)):.1f}%"></i></div>'
        stats.append(
            _stat("Quota used", f"{used:,}", f"/ {limit:,}", bar)
        )

    note = _result_note(deleted, failed)

    if not items:
        body = (
            f'<div class="strip">{"".join(stats)}</div>{note}'
            '<div class="empty">No episodes yet.<br>'
            'Ask the agent for a podcast and it will appear here.</div>'
        )
        return _shell(title=f"{config.SHOW_TITLE} // console", base=base, body=body)

    rows: list[str] = []
    group = None
    selectable = 0
    for episode in items:
        key = _group_key(episode.get("created"))
        if key != group:
            group = key
            rows.append(f'<div class="grouprule">{esc(key)}</div>')

        state = str(episode.get("state") or "")
        episode_id = esc(episode.get("id"))
        title = esc(episode.get("title") or "Untitled")

        bits = [_fmt_date(episode.get("created"))]
        if episode.get("language"):
            bits.append(esc(str(episode["language"]).upper()))
        if state == "done":
            if episode.get("duration_s"):
                bits.append(_hms(episode["duration_s"]))
            if episode.get("size_bytes"):
                bits.append(f"{float(episode['size_bytes']) / 1_048_576:.1f} MB")
        else:
            bits.append(f'<span class="state state-{esc(state)}">{esc(state.upper())}</span>')

        summary = episode.get("summary") or ""
        desc = f'<p class="row-desc">{esc(summary)}</p>' if summary else ""

        busy = state in BUSY_STATES
        if not busy:
            selectable += 1
        box = (
            f'<input type="checkbox" class="pick" name="id" value="{episode_id}"'
            f' aria-label="Select {title}"'
            + (' disabled title="Still rendering — cannot be deleted yet"' if busy else "")
            + ">"
        )
        rows.append(
            f'<div class="row">{box}'
            f'<a class="row-main" href="{base}/{episode_id}">'
            f'<div class="meta">{(" <span class=sep>&#183;</span> ").join(bits)}</div>'
            f'<div class="row-title">{title}</div>'
            f"{desc}</a></div>"
        )

    toolbar = (
        '<div class="bulkbar">'
        '<label class="pickall">'
        f'<input type="checkbox" class="pick-all" id="pick-all"'
        f'{" disabled" if not selectable else ""}>'
        "<span>SELECT ALL</span></label>"
        '<span class="bulk-count" id="bulk-count"></span>'
        '<button class="btn btn-danger" id="bulk-delete" type="submit">'
        "DELETE SELECTED</button>"
        "</div>"
    )

    body = (
        f'<div class="strip">{"".join(stats)}</div>{note}'
        f'<form id="bulk" method="post" action="{base}/delete-selected">'
        f'{toolbar}{"".join(rows)}</form>'
    )
    return _shell(title=f"{config.SHOW_TITLE} // console", base=base, body=body, select=True)


# --------------------------------------------------------------------------
# Episode page
# --------------------------------------------------------------------------


def _render_brief(record: dict[str, Any]) -> str:
    """The agent's brief — what this episode was asked to be."""
    request = record.get("request") or {}
    if not isinstance(request, dict) or not request:
        return (
            '<p class="note">No brief was recorded for this episode. Newer episodes '
            'capture the agent&#8217;s topic, sources and notes here.</p>'
        )
    rows: list[str] = []
    for key, value in request.items():
        if value in (None, "", [], {}):
            continue
        if isinstance(value, (list, tuple)):
            items = "".join(
                f'<li>{_maybe_link(str(v))}</li>' for v in value
            )
            rendered = f"<ul>{items}</ul>"
        elif isinstance(value, str) and "\n" in value:
            rendered = f"<pre>{esc(value)}</pre>"
        else:
            rendered = _maybe_link(str(value))
        css = " class=\"prose\"" if key in ("prompt", "angle", "notes", "topic") else ""
        rows.append(f"<dt>{esc(key)}</dt><dd{css}>{rendered}</dd>")
    return f'<dl class="dl-grid">{"".join(rows)}</dl>'


def _maybe_link(value: str) -> str:
    text = value.strip()
    if text.startswith(("http://", "https://")):
        return f'<a href="{esc(text)}" rel="noreferrer noopener">{esc(text)}</a>'
    return esc(text)


def _render_config(record: dict[str, Any]) -> str:
    generation = record.get("generation") or {}
    if not isinstance(generation, dict) or not generation:
        return ""
    rows = "".join(
        f"<dt>{esc(k.replace('_', ' '))}</dt><dd>{esc(v)}</dd>"
        for k, v in generation.items()
        if v not in (None, "")
    )
    return f'<h2>Generation config</h2><dl class="dl-grid">{rows}</dl>'


def _render_transcript(script: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for turn in script:
        speaker = str(turn.get("speaker", "?"))
        who = "A" if speaker == "HOST_A" else "B"
        css = "turn-a" if speaker == "HOST_A" else "turn-b"
        # Audio tags are direction, not dialogue — dim them so the words read.
        text = esc(turn.get("text", ""))
        text = text.replace("[", '<span class="tag">[').replace("]", "]</span>")
        parts.append(
            f'<div class="turn {css}"><div class="turn-who">HOST {who}</div>'
            f'<div class="turn-text">{text}</div></div>'
        )
        if turn.get("block_break_after"):
            # A div, not an <hr>: Chrome does not render pseudo-elements on
            # <hr>, so the label needs a real child element to live in.
            parts.append('<div class="seam"><span>BLOCK BREAK</span></div>')
    return "".join(parts)


def _render_blocks(blocks: list[dict[str, Any]]) -> str:
    """Per-block render metadata — where a bad seam gets traced."""
    if not blocks:
        return ""
    rendered = [b for b in blocks if b.get("request_id") or b.get("duration_s")]
    if not rendered:
        return ""
    rows = []
    for block in blocks:
        rows.append(
            "<tr>"
            f"<td>{esc(block.get('index'))}</td>"
            f"<td>{esc(block.get('chars'))}</td>"
            f"<td>{esc(block.get('turns'))}</td>"
            f"<td>{esc(block.get('duration_s') or '—')}</td>"
            f"<td>{esc(block.get('chars_per_second') or '—')}</td>"
            f"<td>{esc(block.get('boundary') or '—')}</td>"
            f"<td>{esc(block.get('attempts') or '—')}</td>"
            f"<td>{esc(block.get('request_id') or '—')}</td>"
            "</tr>"
        )
    return (
        "<h2>Render blocks</h2>"
        '<div class="tblwrap"><table><thead><tr>'
        "<th>#</th><th>Chars</th><th>Turns</th><th>Sec</th><th>C/s</th>"
        "<th>Cut</th><th>Tries</th><th>Request id</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
    )


def render_episode(
    meta: dict[str, Any],
    script: list[dict[str, Any]],
    record: dict[str, Any],
    *,
    base: str,
    available: set[str],
    telegram_ready: bool = False,
) -> str:
    episode = meta.get("episode") or {}
    state = str(meta.get("state") or "")
    episode_id = str(meta.get("id") or "")

    bits = [_fmt_date(meta.get("created"), long=True)]
    if meta.get("language"):
        bits.append(esc(str(meta["language"]).upper()))
    if episode.get("duration_human"):
        bits.append(esc(episode["duration_human"]))
    if meta.get("chars"):
        bits.append(f"{int(meta['chars']):,} chars")
    if meta.get("block_count"):
        bits.append(f"{meta['block_count']} blocks")
    if state != "done":
        bits.append(f'<span class="state state-{esc(state)}">{esc(state.upper())}</span>')

    parts = [
        f'<p style="margin:22px 0 0"><a class="back" href="{base}">&#8592; ALL EPISODES</a></p>',
        f'<h1 class="title">{esc(meta.get("title") or "Untitled")}</h1>',
        f'<div class="meta">{(" <span class=sep>&#183;</span> ").join(bits)}</div>',
    ]

    if meta.get("description"):
        parts.append(f'<p class="note">{esc(meta["description"])}</p>')

    if state == "done" and "episode.mp3" in available:
        parts.append(
            # "metadata", not "none": the player shows the real duration on load
            # without pulling the whole mp3 down.
            f'<audio controls preload="metadata" src="{base}/{esc(episode_id)}/episode.mp3"></audio>'
        )
    elif state in ("queued", "validated", "rendering", "mixing"):
        progress = (meta.get("progress") or {}).get("message") or state
        parts.append(
            f'<p class="note">Still rendering &#8212; {esc(progress)}. '
            "Reload this page to check again.</p>"
        )
    elif meta.get("error"):
        parts.append(f'<p class="note">{esc(meta["error"])}</p>')

    links = "".join(
        f'<a class="btn" href="{base}/{esc(episode_id)}/{name}" download '
        f'title="{esc(hint)}">{esc(label)}</a>'
        for name, label, hint in DOWNLOADS
        if name in available
    )
    if links:
        parts.append(f'<div class="dl">{links}</div>')

    if state == "done":
        parts.append(
            _render_delivery(meta, base, episode_id, can_send=telegram_ready)
        )

    parts.append("<h2>Request</h2>")
    parts.append(_render_brief(record))
    parts.append(_render_config(record))

    if script:
        parts.append("<h2>Transcript</h2>")
        parts.append(_render_transcript(script))

    parts.append(_render_blocks(meta.get("blocks") or []))

    if state not in ("queued", "validated", "rendering", "mixing"):
        parts.append(
            '<div class="danger"><form method="post" '
            f'action="{base}/{esc(episode_id)}/delete" '
            "onsubmit=\"return confirm('Delete this episode? The audio, script and "
            'request.yaml are removed permanently.\')">'
            '<button class="btn btn-danger" type="submit">DELETE EPISODE</button>'
            '</form><p class="note">Removes the audio, blocks, script and request.yaml '
            "from disk. There is no undo.</p></div>"
        )

    return _shell(
        title=f"{meta.get('title') or 'Episode'} // {config.SHOW_TITLE}",
        base=base,
        body="".join(parts),
        read=True,
    )


DELIVERY_LABEL = {
    "sent": "SENT", "failed": "FAILED", "skipped": "SKIPPED", "sending": "SENDING",
}


def _render_delivery(meta: dict[str, Any], base: str, episode_id: str, *, can_send: bool) -> str:
    """Telegram delivery status plus a manual send button."""
    record = meta.get("delivery") or {}
    state = str(record.get("state") or "")
    rows = []
    if state:
        css = {"sent": "ok", "failed": "bad", "skipped": "busy"}.get(state, "busy")
        detail = record.get("chat") or record.get("error") or ""
        rows.append(
            f'<dt>status</dt><dd><span class="dstate d-{css}">'
            f'{esc(DELIVERY_LABEL.get(state, state.upper()))}</span>'
            + (f" &#183; {esc(detail)}" if detail else "")
            + "</dd>"
        )
        if record.get("at"):
            rows.append(f'<dt>at</dt><dd>{esc(record["at"])}</dd>')
    else:
        rows.append('<dt>status</dt><dd class="prose">Not sent to Telegram.</dd>')

    button = ""
    if can_send:
        label = "SEND AGAIN" if state == "sent" else "SEND TO TELEGRAM"
        button = (
            f'<form method="post" action="{base}/{esc(episode_id)}/send" class="inline-form">'
            f'<button class="btn" type="submit">{label}</button></form>'
        )
    else:
        button = (
            '<p class="note">Configure a bot token and chat id in '
            f'<a href="{base}/settings">settings</a> to send episodes.</p>'
        )
    return f'<h2>Telegram</h2><dl class="dl-grid">{"".join(rows)}</dl>{button}'


def render_settings(
    view: dict[str, Any],
    *,
    base: str,
    saved: bool = False,
    tested: str = "",
    error: str = "",
) -> str:
    """The Telegram delivery settings form."""
    mask = view.get("telegram_bot_token_mask") or ""
    token_value = esc(MASK_SENTINEL) if view.get("telegram_bot_token_set") else ""
    token_hint = (
        f"Currently <code>{esc(mask)}</code>. Leave as-is to keep it; type a new "
        "token to replace it."
        if view.get("telegram_bot_token_set")
        else "From @BotFather, e.g. <code>123456789:AA…</code>."
    )
    notes = []
    if saved:
        notes.append('<p class="note notice">Settings saved.</p>')
    if tested:
        notes.append(f'<p class="note notice">{esc(tested)}</p>')
    if error:
        notes.append(f'<p class="note notice notice-bad">{esc(error)}</p>')

    auto_on = " checked" if view.get("telegram_auto_send") else ""

    body = f"""
<p style="margin:22px 0 0"><a class="back" href="{base}">&#8592; ALL EPISODES</a></p>
<h1 class="title">Settings</h1>
<div class="meta">TELEGRAM DELIVERY</div>
{"".join(notes)}
<form method="post" action="{base}/settings" class="settings">
  <div class="field">
    <label for="tok">Bot token</label>
    <input type="password" id="tok" name="telegram_bot_token" value="{token_value}"
           autocomplete="off" spellcheck="false" placeholder="123456789:AA…">
    <p class="hint">{token_hint}</p>
  </div>
  <div class="field">
    <label for="chat">Chat id</label>
    <input type="text" id="chat" name="telegram_chat_id"
           value="{esc(view.get("telegram_chat_id") or "")}"
           autocomplete="off" spellcheck="false" placeholder="-1001234567890">
    <p class="hint">A negative id is a group or channel. The bot must already be a
       member (and, for a channel, an admin).</p>
  </div>
  <div class="field field-check">
    <label class="pickall" for="auto">
      <input type="checkbox" class="pick-all" id="auto" name="telegram_auto_send"{auto_on}>
      <span>SEND EVERY NEW EPISODE AUTOMATICALLY</span>
    </label>
    <p class="hint">When on, a finished episode is uploaded to the chat as soon as it
       renders. A delivery failure is recorded on the episode and never fails the
       render.</p>
  </div>
  <div class="actions">
    <button class="btn" type="submit" name="action" value="save">SAVE</button>
    <button class="btn" type="submit" name="action" value="test">SAVE &amp; SEND TEST</button>
  </div>
</form>
<p class="note">The token is stored on the server (chmod 600) and never shown again.
   Setting <code>TELEGRAM_BOT_TOKEN</code> in the environment instead keeps it off
   disk entirely.</p>
"""
    return _shell(title=f"Settings // {config.SHOW_TITLE}", base=base, body=body, read=True)


def render_error(message: str, *, base: str, status: int = 404) -> str:
    return _shell(
        title=f"{status} // {config.SHOW_TITLE}",
        base=base,
        body=(
            f'<div class="empty">{esc(message)}<br>'
            f'<a class="back" href="{base}">&#8592; ALL EPISODES</a></div>'
        ),
    )


def summarize_for_index(meta: dict[str, Any], record: dict[str, Any]) -> str:
    """One line under a title in the list: the description, else the brief's topic."""
    if meta.get("description"):
        return str(meta["description"])
    request = record.get("request") or {}
    if isinstance(request, dict):
        for key in ("angle", "topic", "prompt"):
            if request.get(key):
                return str(request[key])
    return ""
