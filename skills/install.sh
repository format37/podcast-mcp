#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# install.sh — install the podcast skill for Claude Code, with this clone's
# path filled in (the skill points at the worked examples in examples/).
#
#   ./skills/install.sh
#   ./skills/install.sh --zip ~/podcast-skill.zip
#
# Copies and substitutes rather than symlinking, so re-running it after a
# `git pull` re-applies the path on top of the new version.
#
# --zip additionally emits an archive for claude.ai (Customize -> Skills ->
# "+" -> Create skill). Same single source, two targets: the terminal reads
# the installed copy, the web and phone read the uploaded one, and neither
# drifts from the other. claude.ai has no filesystem, so it reads the skill's
# inline script rules instead of the example files; the path is harmless there.
# ---------------------------------------------------------------------------
set -euo pipefail

ZIP=""
if [ "${1:-}" = "--zip" ]; then
    ZIP="${2:?--zip needs an output path}"
fi

REPO="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$REPO/skills/podcast/SKILL.md"
DEST="${SKILLS_DIR:-$HOME/.claude/skills}/podcast"
[ -f "$SRC" ] || { echo "error: $SRC not found" >&2; exit 1; }

mkdir -p "$DEST"
sed "s|/path/to/podcast-mcp|${REPO}|g" "$SRC" > "$DEST/SKILL.md"

if [ -n "$ZIP" ]; then
    # claude.ai wants a zip whose top level is the skill FOLDER, not a bare
    # SKILL.md, and the folder name must match the `name:` in the frontmatter.
    command -v zip >/dev/null || { echo "error: zip is not installed" >&2; exit 1; }
    STAGE=$(mktemp -d)
    mkdir -p "$STAGE/podcast"
    cp "$DEST/SKILL.md" "$STAGE/podcast/SKILL.md"
    rm -f "$ZIP"
    ( cd "$STAGE" && zip -q -r "$ZIP" podcast )
    rm -rf "$STAGE"
    echo "packaged: $ZIP  (claude.ai -> Customize -> Skills -> + -> Create skill)"
fi

echo "installed: $DEST/SKILL.md"
echo "  repo:     $REPO"
echo
echo "Re-run this after a git pull to re-apply the path."
