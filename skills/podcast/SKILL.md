---
name: podcast
description: Write and render a NotebookLM-style two-host podcast episode from any topic, then deliver it to Telegram with an inline player. Use whenever the user wants a podcast, an audio episode, an audio explainer, a "two hosts discussing X" piece, an audio digest of a paper/repo/article, or asks to "turn this into a podcast" / "сделай подкаст". Claude researches the topic and WRITES THE DIALOGUE SCRIPT itself; the podcast MCP server only renders the audio (ElevenLabs Eleven v3 text-to-dialogue), mixes it and stores it. Delivery is a telegram-skill call at the end.
---

# podcast

Turn a topic into a finished two-host episode. The division of labour is fixed
and it is the whole design:

* **You** do the thinking — research, structure, the script, the voices, the
  show notes. There is no LLM inside the server.
* **The `podcast` MCP** does the rendering — validate, chunk, call ElevenLabs,
  mix, store. It is deliberately dumb, so the script is the only contract.
* **The `telegram` skill** delivers the finished mp3.

## The loop

0. **Check what already exists**: `podcast_list`. Each row carries the episode's
   `topic` and `angle`. If anything looks related, read it with
   `podcast_get_script` — its `request_yaml` holds the previous episode's brief,
   sources and notes, often including a "the natural next episode is…" pointer.
   A series that builds beats twelve unconnected episodes.
1. **Research** the topic properly (WebSearch, perplexity, the repo, the
   paper — whatever fits). A podcast script written from memory sounds like one.
2. **Write the script** — a JSON array of turns, rules below.
3. **Check it**: `podcast_estimate(script)`. It validates, chunks, and tells you
   the length and cost. Fix anything it rejects; the errors name the turn and
   the fix.
4. **Render**: `podcast_generate(title=…, script=…, description=…, brief=…)`
   returns a `job_id` immediately. **Always pass `brief`** — see below.
5. **Poll**: `podcast_status(job_id)` until `done`. **Rendering runs at about
   1x realtime** — a 15-minute episode takes ~15 minutes. Do something else and
   check back; do not sit in a tight polling loop.
6. **Deliver** with the telegram skill (the `deliver` field of the done status
   spells out the exact command):

   ```bash
   PY=~/.claude/skills/telegram/.venv/bin/python
   TG=~/.claude/skills/telegram/tg.py
   $PY $TG audio "<path from podcast_status>" \
       --title "<episode title>" --performer "<show name>" \
       --caption "show notes here"
   ```

## Always pass a `brief`

`brief` records *why* the episode exists. It is written to the episode's
`request.yaml` next to the exact generation config, shown in the web console,
and handed to whichever agent plans the next episode — including future you.
Skipping it is how a back catalogue turns into an unnavigable pile.

```json
{"topic":   "ROC curves and AUC for binary classification",
 "prompt":  "<the user's request, verbatim — do not paraphrase>",
 "angle":   "the curve as a menu of thresholds, not a score",
 "sources": ["https://…", "https://…"],
 "notes":   "Follows part 1 on precision/recall. Natural part 3 is PR curves."}
```

* **`prompt` verbatim.** Your paraphrase loses what the user actually wanted.
* **`angle`** is what makes *this* episode distinct — the thing that stops
  episode nine from re-explaining episode two.
* **`notes`** is a letter to the next agent: what the listener already knows,
  what deliberately got left out, what the follow-up should be.
* `sources` should be what you actually read, not a plausible reading list.

Any extra keys you find useful are kept (e.g. `series`, `audience`).

## The web console

A private, server-rendered console lists every episode with its audio player,
brief, generation config, transcript and per-block render metadata, and offers
`episode.mp3` / `script.txt` / `request.yaml` / `script.json` for download.
Locally: **http://localhost:8021/podcast/console**. `podcast_list` returns the
public URL under `console` once the server is deployed. Everything there is also
reachable through the tools, so you never need the browser to read past work.

## Script format — the contract

```json
[
  {"speaker": "HOST_A", "text": "[excited] Okay, so today we're finally doing ROC curves."},
  {"speaker": "HOST_B", "text": "[laughs] The curve everyone plots and nobody reads."},
  {"speaker": "HOST_A", "text": "Right? It's in every model report and nobody reads past the number.",
   "block_break_after": true}
]
```

Enforced by the server — a violation is rejected before any credits are spent:

| rule | limit |
|---|---|
| `speaker` | exactly `HOST_A` or `HOST_B` |
| `text` | 1–500 characters |
| whole script | ≤ 30,000 characters |
| audio tags | inline `[in square brackets]`, must be balanced |
| `block_break_after` | optional boolean, marks a preferred cut |

### `block_break_after` is the one thing you must get right

The script is cut into ~1,800-character render blocks, because that is the
API's per-request ceiling. **Every cut is an audible seam**, and Eleven v3
supports no request stitching, so a seam cannot be smoothed away afterwards —
it can only be *placed well*.

So: mark `block_break_after: true` on the last turn of each subtopic, and write
a real transition line right after it ("Okay, let's switch gears to…", "So
that's the theory — what breaks in practice?"). A seam that lands on a topic
change and a tempo change is a seam nobody notices. Roughly one every 1,500–1,800
characters; the server ignores markers that come sooner than 600 characters in.

## Writing hosts worth listening to

Two distinct people, not one narrator split in half:

* **HOST_A** — the curious one who drives. Asks the question the listener has,
  pushes back, summarises, gets things slightly wrong on purpose.
* **HOST_B** — the one who knows. Concrete examples, real numbers, the caveat
  everyone skips. Explains *to* A, never lectures the audience.

What makes it sound real:

* **Short turns.** 1–3 sentences. Long turns are the number-one thing that makes
  a generated podcast sound generated.
* **Frequent handoffs**, interruptions and back-channels — "right", "wait—",
  "hmm", "hold on". Give A the reactions a listener would have.
* **Cold open**: start mid-thought on the most interesting thing, not
  "Welcome to another episode of…".
* **One idea per turn.** If a turn has two, split it into two turns.
* **Close** with a recap and exactly one takeaway.
* **Vary the rhythm** — a short exchange after a long explanation is what makes
  the long one land.
* Write **numbers as words** where they'd be spoken ("point five", "nine times
  out of ten"): the model reads digits inconsistently.

### Audio tags

Free-form direction in square brackets, applied to the turn from that point on:
`[laughs]`, `[excited]`, `[thoughtful]`, `[whispers]`, `[sighs]`, `[amused]`,
`[surprised]`, `[warm]`, `[groans]`, `[pause]`. Stacking works:
`[whispers][thoughtful]`.

Use them sparingly — roughly one every four or five turns. Tagging every turn
makes the delivery hammy and eats the character budget (tags are billed as text).
A tag must fit what the words actually do; `[laughs]` on a line that isn't funny
sounds broken.

## Length, cost, budget

Measured on real renders. **The speech rate depends heavily on the language**,
so budget with the right row:

| language | chars/second | chars per minute of audio |
|---|---|---|
| English | 14.1 | ~850 |
| Russian | 13.2 | ~790 |
| **Japanese** | **5.6** | **~340** |

| | |
|---|---|
| credits | **~0.55 per script character** |
| file size | ~0.69 MB/min (mono 96 kbps) |
| render time | ~1x realtime |

So in English a **10-minute** episode ≈ 8,500 chars ≈ 4,700 credits, and a
**25-minute** one ≈ 21,000 chars ≈ 11,500 credits and ~17 MB. **A Japanese
script for the same 10 minutes is only ~3,400 characters** — roughly 2.5x
cheaper per minute, and it is easy to write far too much by eye. Always check
with `podcast_estimate`, which uses the per-language rate and tells you whether
that language is calibrated.

The account (Creator, 121k characters/month) is good for roughly **3.5 hours of
audio a month**. Check what's left with `podcast_list` before committing to a
long one, and prefer 10–15 minutes unless the user asks for more — it is also
simply a better length for a two-host explainer.

Keep episodes under **20 MB** (~28 min) if they will be sent by URL rather than
uploaded; `podcast_status` reports `telegram_url_delivery_ok`.

## Languages

English, Russian and Japanese are all verified working and calibrated. Pass
`language="ru"` / `"ja"` — it pins the language for the model, picks the right
speech rate for estimates, and sets the right duration sanity window.

* **Always write natively — never translate an English script.** The register
  differs: Russian tolerates less banter and more directness; Japanese wants
  shorter turns still, and the back-channels ("なるほど", "そう") do more work.
* **Audio tags stay in English** (`[laughs]`, `[curious]`, `[thoughtful]`) even
  inside a Russian or Japanese script. They are model directives, not dialogue —
  verified working in both. Worked examples:
  `examples/hallucinations_ru.json`, `examples/hallucinations_ja.json`.
* The default voices are multilingual premades and carry a slight English accent
  in ru/ja. If that bothers the user, audition with `podcast_voices` and pass
  `voice_a` / `voice_b`.
* **Another language?** It will render — v3 auto-detects — but its rate is
  uncalibrated, so length estimates are rough and the sanity window falls back
  to a wide default. `podcast_estimate` reports
  `rate_calibrated_for_language: false`. Measure the real rate from
  `podcast_get_script` and add it to `LANG_CPS`.

## Tools

| tool | use |
|---|---|
| `podcast_estimate(script, language)` | validate + cost, mid-draft. Free. |
| `podcast_generate(title, script, language, description, voice_a, voice_b, brief, dry_run)` | render; returns `job_id` |
| `podcast_status(job_id)` | progress; on `done`, the path, size and delivery command |
| `podcast_list(limit)` | recent episodes with topic/angle + remaining quota |
| `podcast_get_script(episode_id, include_transcript)` | a past episode's brief, `request_yaml`, script and per-block render metadata |
| `podcast_voices()` | available voices for casting the hosts |
| `podcast_delete(episode_id)` | remove an episode |

## Worked example

In the repo's `examples/` directory (when installing this skill outside the
checkout, point the paths below at your clone):

* `roc_curves_en.json` — a full 3.5-minute English episode. **Read this before
  writing your first script.** Note the turn lengths, the two
  `block_break_after` markers with their transition lines, and how sparse the
  audio tags are.
* `hallucinations_ru.json` / `hallucinations_ja.json` — short single-block
  episodes showing native-language writing with English audio tags.

## Gotchas

* **Don't poll tightly.** A 20-minute episode renders in ~20 minutes.
* **A turn over 500 chars is rejected** — split it into two consecutive turns by
  the same speaker. That is also better writing.
* **Unbalanced brackets get read aloud.** The validator catches them.
* **`dry_run=true` costs nothing** — use it whenever unsure.
* **The server never delivers anything.** The episode is not sent until you call
  the telegram skill.
