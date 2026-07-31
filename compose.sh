#!/usr/bin/env bash
# Build + (re)start the podcast MCP locally on 127.0.0.1:8021.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then
    echo "error: .env is missing — copy .env.example and set ELEVENLABS_API_KEY" >&2
    exit 1
fi

docker compose up -d --build "$@"
echo "podcast MCP -> http://localhost:8021/podcast/"
echo "health      -> curl -s http://localhost:8021/health"
