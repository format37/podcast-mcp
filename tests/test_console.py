"""Console rendering — output correctness, and escaping of agent-authored text.

Every string on these pages (titles, briefs, transcripts) is written by an
agent from material it found while researching. That is untrusted input on the
way to a browser, so escaping is a correctness property with a test, not an
assumption.
"""

from __future__ import annotations

import brief
import console

META = {
    "id": "20260731-abcdef123456",
    "title": "ROC Curves, Read Properly",
    "created": "2026-07-31T10:00:00Z",
    "description": "Show notes here.",
    "language": "en",
    "state": "done",
    "chars": 2986,
    "block_count": 3,
    "model_id": "eleven_v3",
    "voice_a": "va",
    "voice_b": "vb",
    "episode": {"duration_s": 211.12, "duration_human": "3m 31s", "size_mb": 2.42},
    "blocks": [
        {"index": 0, "chars": 1346, "turns": 12, "boundary": "marker",
         "duration_s": 92.76, "chars_per_second": 14.51, "attempts": 1, "request_id": "req0"},
    ],
    "progress": {"message": "done"},
}

SCRIPT = [
    {"speaker": "HOST_A", "text": "[excited] First line.", "block_break_after": True},
    {"speaker": "HOST_B", "text": "Second line."},
]

ALL_FILES = {"episode.mp3", "script.txt", "request.yaml", "script.json"}


def page(meta=None, script=None, brief_data=None, available=None):
    meta = {**META, **(meta or {})}
    record = brief.build(meta, brief_data)
    return console.render_episode(
        meta, SCRIPT if script is None else script, record,
        base="/podcast/console", available=ALL_FILES if available is None else available,
    )


# ── index ───────────────────────────────────────────────────────────────────


def test_index_lists_episodes_newest_first_with_month_rules():
    items = [
        {"id": "20260731-a", "title": "July one", "state": "done", "created": "2026-07-31T10:00:00Z",
         "language": "en", "duration_s": 100, "size_bytes": 1_000_000, "summary": "s1"},
        {"id": "20260610-b", "title": "June one", "state": "done", "created": "2026-06-10T10:00:00Z",
         "language": "ru", "duration_s": 50, "size_bytes": 500_000, "summary": "s2"},
    ]
    html = console.render_index(items, base="/podcast/console")
    assert "JUL 2026" in html and "JUN 2026" in html
    assert html.index("July one") < html.index("June one")
    assert '/podcast/console/20260731-a' in html


def test_index_shows_state_for_unfinished_episodes():
    items = [{"id": "20260731-a", "title": "Rendering", "state": "rendering",
              "created": "2026-07-31T10:00:00Z", "language": "en", "summary": ""}]
    html = console.render_index(items, base="/podcast/console")
    assert "RENDERING" in html and "state-rendering" in html


def test_index_totals_only_finished_episodes():
    items = [
        {"id": "20260731-a", "title": "done", "state": "done", "created": "2026-07-31T10:00:00Z",
         "duration_s": 120, "size_bytes": 1_048_576, "summary": ""},
        {"id": "20260731-b", "title": "failed", "state": "failed", "created": "2026-07-31T10:00:00Z",
         "duration_s": 999, "size_bytes": 99_999_999, "summary": ""},
    ]
    html = console.render_index(items, base="/podcast/console")
    assert "2m 00s" in html  # the failed episode's phantom duration is excluded
    assert "1.0" in html


def test_empty_index_says_so():
    html = console.render_index([], base="/podcast/console")
    assert "No episodes yet" in html


# ── selection / bulk delete ─────────────────────────────────────────────────


def items(*states):
    return [
        {"id": f"2026073{i}-{'abcdef123456'}", "title": f"Ep {i}", "state": state,
         "created": "2026-07-31T10:00:00Z", "language": "en",
         "duration_s": 60, "size_bytes": 1000, "summary": ""}
        for i, state in enumerate(states)
    ]


def test_each_row_has_a_checkbox_carrying_its_id():
    html = console.render_index(items("done", "done"), base="/podcast/console")
    assert html.count('class="pick"') == 2
    assert 'name="id" value="20260730-abcdef123456"' in html


def test_select_all_and_delete_button_are_present():
    html = console.render_index(items("done"), base="/podcast/console")
    assert 'id="pick-all"' in html
    assert 'id="bulk-delete"' in html
    assert "SELECT ALL" in html


def test_the_list_is_one_form_posting_to_delete_selected():
    html = console.render_index(items("done"), base="/podcast/console")
    assert 'method="post" action="/podcast/console/delete-selected"' in html
    assert 'id="bulk"' in html


def test_form_action_keeps_the_token_prefix():
    html = console.render_index(items("done"), base="/podcast/SECRET/console")
    assert 'action="/podcast/SECRET/console/delete-selected"' in html


def test_rendering_episodes_cannot_be_selected():
    """Deleting mid-render would pull files out from under the worker."""
    html = console.render_index(items("rendering", "done"), base="/podcast/console")
    assert "disabled title=\"Still rendering" in html
    assert html.count("<input type=\"checkbox\" class=\"pick\"") == 2
    assert html.count("disabled") >= 1


def test_select_all_is_disabled_when_nothing_is_selectable():
    html = console.render_index(items("rendering", "queued"), base="/podcast/console")
    assert 'id="pick-all" disabled' in html


def test_selection_script_is_only_on_the_index():
    """The episode page has no list to select from; don't ship it the code."""
    assert "pick-all" not in page()
    assert "pick-all" in console.render_index(items("done"), base="/podcast/console")


def test_checkboxes_are_labelled_for_screen_readers():
    html = console.render_index(items("done"), base="/podcast/console")
    assert 'aria-label="Select Ep 0"' in html


def test_checkbox_label_is_escaped():
    rows = items("done")
    rows[0]["title"] = '"><script>alert(1)</script>'
    html = console.render_index(rows, base="/podcast/console")
    assert "<script>alert" not in html


def test_row_link_is_separate_from_the_checkbox():
    """A checkbox inside the <a> would navigate away instead of selecting."""
    html = console.render_index(items("done"), base="/podcast/console")
    assert '<div class="row"><input type="checkbox"' in html
    assert '<a class="row-main"' in html


def test_delete_result_is_reported():
    html = console.render_index(items("done"), base="/podcast/console", deleted=3, failed=1)
    assert "Deleted 3 episodes." in html
    assert "1 could not be deleted" in html


def test_singular_wording_for_one_deletion():
    html = console.render_index(items("done"), base="/podcast/console", deleted=1)
    assert "Deleted 1 episode." in html


def test_no_notice_without_a_delete():
    assert "notice" not in console.render_index(items("done"), base="/podcast/console")


def test_notice_shows_on_an_emptied_console():
    html = console.render_index([], base="/podcast/console", deleted=2)
    assert "Deleted 2 episodes." in html
    assert "No episodes yet" in html


def test_quota_bar_is_clamped():
    items = []
    html = console.render_index(
        items, base="/podcast/console",
        quota={"characters_used": 500_000, "characters_limit": 121_020},
    )
    assert "width:100.0%" in html  # over quota must not draw past the bar


# ── episode page ────────────────────────────────────────────────────────────


def test_episode_page_has_player_downloads_and_transcript():
    html = page()
    assert "<audio" in html and "/episode.mp3" in html
    for name in ALL_FILES:
        assert f"/{name}" in html
    assert "First line." in html and "Second line." in html
    assert "REQUEST.YAML" in html


def test_only_existing_files_are_offered():
    html = page(available={"episode.mp3"})
    assert "REQUEST.YAML" not in html
    assert "TRANSCRIPT" not in html


def test_unrendered_episode_has_no_player_but_shows_progress():
    html = page(meta={"state": "rendering", "episode": None,
                      "progress": {"message": "block 2/3"}})
    assert "<audio" not in html
    assert "block 2/3" in html


def test_failed_episode_shows_its_error():
    html = page(meta={"state": "failed", "episode": None,
                      "error": "block 1 failed after 3 attempts"})
    assert "block 1 failed after 3 attempts" in html


def test_delete_form_is_absent_while_rendering():
    """Deleting mid-render would pull files out from under the worker."""
    assert "DELETE EPISODE" not in page(meta={"state": "rendering", "episode": None})
    assert "DELETE EPISODE" in page()


def test_seam_marker_is_a_div_not_an_hr():
    """Chrome does not render pseudo-elements on <hr>, so the label needs a child."""
    html = page()
    assert '<div class="seam"><span>BLOCK BREAK</span></div>' in html
    assert '<hr class="seam">' not in html


def test_audio_tags_are_visually_separated_from_dialogue():
    html = page()
    assert '<span class="tag">[excited]</span>' in html


def test_block_table_reports_render_metadata():
    html = page()
    # Headings are uppercased by CSS (text-transform), not in the markup.
    assert "Render blocks" in html
    for value in ("1346", "92.76", "14.51", "marker", "req0"):
        assert str(value) in html


def test_brief_renders_sources_as_links():
    html = page(brief_data={"topic": "t", "sources": ["https://example.com/paper"]})
    assert '<a href="https://example.com/paper"' in html
    assert 'rel="noreferrer noopener"' in html


def test_missing_brief_is_explained_not_blank():
    html = page(brief_data=None)
    assert "No brief was recorded" in html


# ── escaping ────────────────────────────────────────────────────────────────

XSS = '<script>alert("x")</script>'


def test_title_is_escaped():
    html = page(meta={"title": XSS})
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html


def test_transcript_text_is_escaped():
    html = page(script=[{"speaker": "HOST_A", "text": XSS}])
    assert "<script>alert" not in html


def test_brief_values_are_escaped():
    html = page(brief_data={"topic": XSS, "notes": XSS})
    assert "<script>alert" not in html


def test_a_javascript_url_in_sources_is_not_linkified():
    html = page(brief_data={"sources": ["javascript:alert(1)"]})
    assert 'href="javascript:' not in html


def test_description_is_escaped():
    assert "<script>alert" not in page(meta={"description": XSS})


def test_index_escapes_titles_and_summaries():
    html = console.render_index(
        [{"id": "20260731-a", "title": XSS, "state": "done",
          "created": "2026-07-31T10:00:00Z", "summary": XSS}],
        base="/podcast/console",
    )
    assert "<script>alert" not in html


# ── shell ───────────────────────────────────────────────────────────────────


def test_pages_are_marked_noindex():
    """A private console must never end up in a search index."""
    assert 'name="robots" content="noindex, nofollow"' in page()
    assert 'name="robots" content="noindex, nofollow"' in console.render_index([], base="/x")


def test_theme_preference_is_shared_with_the_sibling_sites():
    html = page()
    assert "portfolio-theme" in html
    # Pre-paint must come before the stylesheet or the wrong theme flashes.
    assert html.index("portfolio-theme") < html.index("console.css")


def test_stylesheet_url_is_cache_busted():
    assert f"console.css?v={console.ASSET_V}" in page()


def test_base_prefix_is_used_for_every_link():
    """Token-mode pages must keep the token in every URL they emit."""
    meta = dict(META)
    html = console.render_episode(
        meta, SCRIPT, brief.build(meta, None),
        base="/podcast/SECRET/console", available=ALL_FILES,
    )
    assert "/podcast/SECRET/console/assets/console.css" in html
    assert "/podcast/SECRET/console/20260731-abcdef123456/episode.mp3" in html
    assert 'href="/podcast/console"' not in html


# ── favicon ─────────────────────────────────────────────────────────────────


def test_pages_declare_the_favicon():
    for html in (page(), console.render_index([], base="/podcast/console")):
        assert 'rel="icon" type="image/svg+xml"' in html
        assert "favicon.svg" in html


def test_favicon_links_are_cache_busted():
    html = console.render_index([], base="/podcast/console")
    assert f"favicon.svg?v={console.ASSET_V}" in html


def test_favicon_links_keep_the_token_prefix():
    html = console.render_index([], base="/podcast/SECRET/console")
    assert "/podcast/SECRET/console/assets/favicon.svg" in html


def test_png_and_apple_touch_fallbacks_are_offered():
    html = console.render_index([], base="/podcast/console")
    assert "favicon-32.png" in html
    assert 'rel="apple-touch-icon"' in html


def test_favicon_files_exist_and_are_small():
    import pathlib
    static = pathlib.Path(__file__).resolve().parent.parent / "backend" / "static"
    for name, ceiling in (("favicon.svg", 4_000), ("favicon-32.png", 4_000),
                          ("apple-touch-icon.png", 20_000)):
        path = static / name
        assert path.is_file(), f"{name} is missing"
        assert path.stat().st_size < ceiling, f"{name} is unexpectedly large"


def test_favicon_svg_adapts_to_the_browser_theme():
    """A fixed fill disappears against one of the two tab-strip colours."""
    import pathlib
    svg = (pathlib.Path(__file__).resolve().parent.parent
           / "backend" / "static" / "favicon.svg").read_text(encoding="utf-8")
    assert "prefers-color-scheme: dark" in svg
    assert "viewBox" in svg
