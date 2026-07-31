# podcast-mcp

An MCP server that renders NotebookLM-style **two-host podcast episodes** from a
dialogue script, using the ElevenLabs **Eleven v3 text-to-dialogue** API. It
chunks the script, renders it, mixes it with ffmpeg, stores it, and serves a
private web console for browsing the back catalogue.

**The agent writes the script; the server only renders it.** No LLM calls happen
inside this server. That keeps the renderer swappable (Gemini TTS, local models)
behind a stable tool surface, and keeps the prompt engineering in a skill you can
iterate without redeploying.

```
agent (research + script) ──► podcast_generate ──► job_id
                                    │
                      validate → chunk → render → mix → store
                                    │
                      podcast_status ──► path + download URL
```

## Quick start

```bash
cp .env.example .env          # set ELEVENLABS_API_KEY
./compose.sh                  # → http://localhost:8021/podcast/
```

Add it to Claude Code as an HTTP MCP server at `http://localhost:8021/podcast/`.
The same pipeline runs without the server:

```bash
python backend/render.py script.json out.mp3 --dry-run
python backend/render.py script.json out.mp3 --language ru
```

## Tools

| tool | |
|---|---|
| `podcast_generate` | validate + chunk + render; returns a `job_id` immediately |
| `podcast_status` | `queued → validated → rendering (i/N) → mixing → done` / `failed` |
| `podcast_list` | recent episodes with topic/angle + remaining character quota |
| `podcast_get_script` | a past episode's brief, `request.yaml`, script and per-block render metadata |
| `podcast_estimate` | cost and length without rendering |
| `podcast_voices` | account voices, for casting the two hosts |
| `podcast_delete` | remove an episode |

## Script format

A JSON array of turns — the only contract between agent and server:

```json
[{"speaker": "HOST_A", "text": "[excited] Okay, so today we're doing ROC curves."},
 {"speaker": "HOST_B", "text": "[laughs] The curve everyone plots and nobody reads.",
  "block_break_after": true}]
```

`speaker` is `HOST_A` or `HOST_B`; `text` is 1–500 chars with inline `[audio tags]`;
the script caps at 30,000 chars. Violations are rejected with a message naming
the turn and the fix, before any credits are spent.

**`block_break_after` is the one thing worth getting right.** The script is cut
into ~1,800-char render blocks because that is the API's per-request ceiling, and
every cut is an audible seam that *cannot* be smoothed over — Eleven v3 has no
request stitching. Mark the last turn of each subtopic and write a transition
line after it, so seams land where the tempo changes anyway.

## Web console

A private console at `/podcast/console` lists the whole catalogue on one page —
no pagination — with per-episode checkboxes, select-all and bulk delete. Each
episode page has the audio player, the brief, the generation config, the
transcript, per-block render metadata, and downloads for `episode.mp3`,
`script.txt`, `request.yaml` and `script.json`.

It is server-rendered from this same process: no build step, no second
container, no API in between, so it cannot drift from the data it displays.
JavaScript is limited to the theme toggle and selection niceties, both
progressive enhancement.

With `DOWNLOAD_TOKEN` set the console moves to `/podcast/<DOWNLOAD_TOKEN>/console`
and the unprefixed path 404s. That is deliberately a *different* secret from
`MCP_TOKENS`: a browser following a link and Telegram fetching an mp3 both
arrive without an `Authorization` header, so sharing a console URL must never
leak the credential that can drive the server.

## request.yaml

Every episode carries a reproducible record — the agent's brief (`topic`,
`prompt`, `angle`, `sources`, `notes`), the **exact** generation config (voices,
model, stability, chunking, mixing, encoding), and the result. Config defaults
drift, so an episode is only reproducible if the settings travelled with it.

`podcast_list` surfaces each episode's topic and angle and `podcast_get_script`
returns the full record, so an agent planning episode twelve can build on episode
three instead of repeating it.

## Measured numbers

ElevenLabs documents none of these; all are config knobs.

| | |
|---|---|
| speech rate | **en 14.1** · **ru 13.2** · **ja 5.6** chars/second |
| credits | **~0.55 per script character** |
| size | 0.69 MB/min (mono 96 kbps) |
| render time | ~1× realtime |

**The rate must stay per-language** (`LANG_CPS`). Japanese packs ~2.5× more
speech into a character than English, so a global sanity window derived from
English rejects every *correct* Japanese block, retries each at full credit cost
and fails the job. `tests/test_rates.py` pins this.

A 25-minute English episode is ~21k chars, ~11.5k credits and ~17 MB — under
Telegram's 20 MB send-by-URL limit, which is what drove mono 96 kbps.

## Two findings that shaped the design

**No request stitching.** `/v1/text-to-dialogue` has no `previous_request_ids`,
and ElevenLabs documents stitching as unavailable for `eleven_v3`. Block N+1
cannot be conditioned on block N, so seam quality is decided entirely by *where*
the cut lands — hence the chunker prefers the writer's `block_break_after`
markers over packing blocks full.

**The per-request limit is 2,000 characters**, not 3,000 as commonly assumed
(summed across `inputs[].text`).

## Design notes

**Job state is the episode directory.** No job table: `job_id == episode_id`, and
the state machine is one atomically-rewritten `meta.json`. A restart loses the
queue but never the history, and jobs caught mid-render are reaped into
`interrupted` at startup rather than lying about progress forever.

```
data/episodes/<id>/
    meta.json      state machine + per-block render metadata
    script.json    exactly what the agent submitted
    request.yaml   brief + config + result
    script.txt     the dialogue as readable text
    episode.mp3    the finished mix
    blocks/000.mp3 per-block renders — the only way to debug a seam later
```

**Blocks render sequentially.** The account is metered and v3 is flaky: a failure
on block 4 of 12 should cost four blocks of credits, not twelve. Each block is
persisted before the next starts.

## Deployment

`docker-compose.yml` runs it locally on `127.0.0.1:8021` with auth disabled.
`docker-compose.vps.yml` runs it behind Caddy on the external `mcp-shared`
network with `MCP_REQUIRE_AUTH=true`; point a `handle /podcast* { reverse_proxy
mcp-podcast:8021 }` block at it. Set `MCP_TOKENS` and `DOWNLOAD_TOKEN` in `.env`
on the host (`openssl rand -hex 24`).

## Tests

```bash
python -m pytest      # 107 tests: chunker, validation, rates, request.yaml, console
```

## Layout

| | |
|---|---|
| `backend/config.py` | every knob, with the reasoning for each default |
| `backend/script_model.py` | the script contract; collects *all* errors so a bad script takes one round trip |
| `backend/chunker.py` | block packing and seam placement |
| `backend/tts.py` | ElevenLabs client, backoff, quota |
| `backend/mixing.py` | ffmpeg join → two-pass loudnorm → mono mp3 → ID3 |
| `backend/pipeline.py` | the state machine shared by CLI and server |
| `backend/episodes.py` | episode/job store |
| `backend/brief.py` | `request.yaml` + transcript generation |
| `backend/console.py` | the web console |
| `backend/main.py` | FastMCP server, tools, routes, token auth |
| `backend/render.py` | standalone CLI |

The agent-side half — the dialogue prompt template, per-language style notes and
the worked example — lives in `skills/podcast/SKILL.md`.

## Licence

MIT (see `LICENSE`). The bundled JetBrains Mono and Inter woff2 subsets in
`backend/static/fonts/` are licensed under the SIL Open Font License 1.1.
