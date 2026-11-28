# AdReel

Raw phone clips in → a branded, ready-to-post 9:16 TikTok/Instagram video out, with burned-in
Amharic captions, price card, logo and call-to-action end card. Built for small businesses in
Ethiopia and delivered as a Telegram bot.

| Phase | Scope | Status |
|---|---|---|
| 1 | Render engine + CLI (clips + edit plan + brand kit → video) | done |
| 2 | AI layer: Scribe transcription, Gemini edit plan, Azure/ElevenLabs TTS, cost log | done |
| 3 | Telegram bot (aiogram 3), Redis/RQ worker, Postgres, preview/approve/fix-captions | **done** |
| 4 | Credits ledger, Chapa payments, admin `/stats` | |

## Quick start (local)

Requirements: Python 3.11+, ffmpeg built with libass, libx264 and libzimg (Debian/Ubuntu
`apt install ffmpeg` is fine). For face-aware reframing also `apt install libegl1 libgles2`.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[face,dev]"
adreel samples -o examples/media          # synthetic clips, logo and a test "voiceover"
cd examples
adreel render -p plan_product_promo.json -b brand_kit.json \
  -c c1=media/c1_portrait_60fps.mp4 -c c2=media/c2_landscape_noaudio.mp4 \
  -c c3=media/c3_landscape_25fps.mp4 -o promo.mp4
adreel render -p plan_talking_head.json -b brand_kit.json \
  -c c2=media/c2_landscape_noaudio.mp4 -c c3=media/c3_landscape_25fps.mp4 \
  -o talk_preview.mp4 -q preview --watermark --voiceover media/vo_test.wav
```

**Automatic editing: just say what you want.** The brief is in your own words, Amharic or
English. The system then:
- picks the template from the footage
- takes the price from your words
- writes all the on-screen Amharic text, the hook and the call to action (Gemini)
- cuts the video and corrects the colour

`--fake` runs it without API keys, using fake transcription and TTS plus the heuristic
planner; drop it once your keys are in `.env`.

```bash
cd examples
adreel make -b brand_kit.json \
  -c c1=media/c1_portrait_60fps.mp4 -c c2=media/c2_landscape_noaudio.mp4 \
  -c c3=media/c3_landscape_25fps.mp4 \
  --brief "የይርጋጨፌ ቡና ማስታወቂያ ነው፤ ዋጋው 1200 ብር፤ ወጣቶችን እንዲማርክ ፈጣንና ደማቅ አድርጉት" \
  --voice standard -o out/promo.mp4 --fake
# -> out/promo.mp4, out/promo.plan.json (edit and re-render with `adreel render`), out/promo.cost.json
```

With Docker:

```bash
docker build -t adreel .
docker run --rm -v "$PWD/examples:/work" -w /work adreel render -p plan_product_promo.json \
  -b brand_kit.json -c c1=media/c1_portrait_60fps.mp4 -c c2=media/c2_landscape_noaudio.mp4 \
  -c c3=media/c3_landscape_25fps.mp4 -o promo.mp4
```

### CLI

| Command | What it does |
|---|---|
| `adreel make -b BRAND -c ID=FILE… --brief "what you want" [-t auto\|product_promo\|talking_head] [--product --price --old-price --highlight --target --cta --notes] [--voice none\|standard\|premium] [-q] [--watermark] [--fake]` | Fully automatic: transcribe → plan → voiceover → colour → render, plus the plan and cost files |
| `adreel revise -p PLAN -b BRAND -c ID=FILE… --prompt "what to change" [--brief] [--voice] [-o]` | Change an existing edit with a prompt (needs Gemini) |
| `adreel transcribe FILE [-l am] [-o JSON] [--fake]` | Word-level transcript, printed as caption lines |
| `adreel bot` | Run the Telegram bot (long polling) |
| `adreel worker [--burst]` | Run an RQ render worker |
| `adreel db upgrade [REV]` / `adreel db downgrade REV` | Database migrations |
| `adreel render -p PLAN -b BRAND -c ID=FILE… -o OUT [-q preview\|final] [--watermark] [--voiceover WAV] [--no-repair] [--no-face]` | Render a video |
| `adreel validate -p PLAN [-c ID=FILE…] [--repair]` | Schema check; with clips, also checks durations and the template, and can print the auto-repaired plan |
| `adreel schema [-o FILE]` | Dump the edit-plan JSON Schema (the contract Gemini fills in Phase 2) |
| `adreel templates` | List templates |
| `adreel samples [-o DIR]` | Generate synthetic test media |

`-q preview` renders 540×960 at ~1 Mbit/s (a 30 s preview is ~2–4 MB, for slow mobile
connections). `-q final` renders 1080×1920 at CRF 19 (sent as a document so Telegram does not
recompress it).

## Telegram bot (Phase 3)

### Run it

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. `cp .env.example .env`, then set `ADREEL_TELEGRAM_BOT_TOKEN` and your AI keys. With
   no AI keys, add `ADREEL_TRANSCRIBER=fake`, `ADREEL_PLANNER=heuristic` and
   `ADREEL_TTS_STANDARD=fake` to try the flow.

**Everything in one process.** Works on Windows, with no Docker, Redis or Postgres:

```bash
adreel db upgrade        # creates adreel.db (SQLite)
adreel bot               # ADREEL_QUEUE_BACKEND=inline: renders run on a background thread
```

**Production-like stack.** Docker Compose with Postgres, Redis, migrations, the bot and an
RQ worker:

```bash
docker compose up --build
docker compose up --scale worker=3     # more parallel renders
```

For videos over 20 MB, run the self-hosted Bot API server. Set its keys in `.env`
(`TELEGRAM_API_ID/HASH`, `ADREEL_TELEGRAM_API_BASE`, `ADREEL_TELEGRAM_LOCAL_MODE`), then
start it with `docker compose --profile local-api up`.

### What the user sees

```
/start  → language → business name → type → logo (or skip) → colour → phone → Telegram handle
/new    → template (or 🤖 let the AI choose) → send 1–5 clips (Done)
        → "tell us what you want" (one free-text message; include the price for a promo)
        → voice
        → "⏳ Checking your clips… 35% ▓▓▓░░░░░░░" (one message, edited as the job runs)
        → preview video  [✅ Approve] [💬 Change something] [✏️ Fix captions] [🔁 Redo]
Change  → "what should we change?" e.g. "show the price first, make it shorter, warmer colours"
        → the AI revises the current edit (only what was asked) → new preview, with a note if
          something couldn't be done
Fix     → numbered captions; reply "3: corrected text" (several lines at once) → re-render, no AI
Redo    → a fresh AI edit (Redo and Change share 2 free re-edits per video)
Approve → full-quality MP4 sent as a document (no Telegram recompression)
/brand  /lang  /help  /cancel
```

Messages are in Amharic by default, with English via `/lang`. The catalogue is
`src/adreel/i18n.py`; have a native speaker review it before launch.

### Architecture

```
Telegram ⇄ bot (aiogram 3, async) ──writes──▶ Postgres (users, brand_kits, projects, clips, jobs, api_costs)
              │ enqueue job id                       ▲
              ▼                                      │ status, plan, costs
           Redis/RQ ──▶ worker (sync) ── downloads clips from Telegram once → storage (local/S3)
                                  └── AutoEditor / Renderer → upload preview/final → Telegram
```

- **The bot never renders.** It only records Telegram file IDs, so it stays fast on slow
  connections. Workers download clips once and keep them in storage, so caption fixes,
  redos and the final render never download again.
- **Interrupted uploads are resumable.** Clips already received stay with the draft
  project, and `/new` offers to continue.
- **Jobs:**
  - `auto_edit`: the AI pipeline, ending in a preview
  - `rerender`: the stored plan after caption fixes, ending in a preview
  - `final`: full quality, delivered as a document

  Each job has a hard timeout (`ADREEL_JOB_TIMEOUT_S`). Transient failures (provider
  429/5xx, network, Telegram) are retried by RQ, with backoff up to `ADREEL_JOB_RETRIES`
  times. Other failures end the job with a clear message to the user. A failed re-render
  keeps the earlier preview's buttons working.
- **Limits:**
  - one active job per user
  - `ADREEL_RATE_LIMIT_VIDEOS_PER_HOUR`
  - a flood guard on messages and taps
  - clip count, length and size checks when clips arrive, and again after download
- **Storage** sits behind `storage.Storage`: local disk or any S3-compatible service
  (AWS, Cloudflare R2, MinIO). Install with `pip install -e ".[s3]"`.
- **Migrations:** Alembic, run as `adreel db upgrade`. In Compose a one-shot `migrate`
  service runs them before the bot and worker start.
- **Worker platform:** RQ workers need `fork()`, so they run on Linux and macOS (Docker on
  Windows). On Windows without Docker, use `ADREEL_QUEUE_BACKEND=inline`.

## The edit plan

`src/adreel/schemas/edit_plan.py` is the contract between the AI layer and the renderer. The
generated JSON Schema is in [`docs/edit_plan.schema.json`](docs/edit_plan.schema.json) and
the examples are in [`examples/`](examples).

```jsonc
{
  "schema_version": "1.0",
  "template_id": "product_promo",          // which declarative template
  "language": "am",                        // "am" | "en" (UI labels like "ብር"/"ETB")
  "segments": [                            // ordered; the output is their concatenation
    {"id": "s1", "clip_id": "c1", "start": 0.5, "end": 3.5, "role": "hook",
     "reframe": {"mode": "auto", "focus_x": 0.5, "focus_y": 0.5, "zoom": 1.0},
     "keep_audio": true, "note": "why this cut (ignored by renderer)"}
  ],
  "captions": [                            // SOURCE time: seconds inside clip_id
    {"id": 1, "text": "ዛሬ አዲስ ቡና ደርሷል!", "timebase": "source", "clip_id": "c1",
     "start": 0.6, "end": 3.3, "words": [{"text": "ዛሬ", "start": 0.6, "end": 0.95}]}
  ],
  "on_screen_text": [{"text": "ትኩስ ቡና", "segment_id": "s2", "start_offset": 0.3,
                      "duration": 2.5, "style": "label", "position": "top"}],
  "slots": {"hook_text": "…", "product_name": "…", "price": 1200, "old_price": 1500,
            "price_note": "ለ3 ቀን ብቻ", "cta_text": "አሁኑኑ ይዘዙ!"},
  "price_card": {"enabled": true, "segment_id": "s4", "start_offset": 0.4, "duration": 3.2},
  "voiceover": {"script": "…", "voice": "standard", "start": 0.0, "audio_path": null}
}
```

Key decisions:

- **Captions are source-anchored.** They are in the transcript's own time, so the LLM never
  computes output timestamps. The renderer maps them through the cuts:
  - A caption split by a removed silence is re-joined.
  - A word that is mostly cut away (a filler like `እ...`) disappears from the text.
  - A caption interrupted by an inserted b-roll splits into two events.

  Captions generated in output time (voiceover captions from TTS word boundaries in Phase 2)
  use `timebase: "output"`.
- **Overlays and the price card are segment-anchored** (`segment_id` + offset), which LLMs
  handle reliably.
- **The schema is strict and Gemini-friendly.** Every object has `additionalProperties:
  false`, enums are plain strings, there are no free-form maps, and slots are explicit fields.
- **Validation happens at two levels.** Pydantic checks the shape. `plan_checks.py` checks
  the plan against reality (clip durations, template length, required slots) and repairs
  common slips deterministically: it clamps out-points, drops segments outside a clip, trims
  to the template's max length and re-targets dangling references. Phase 2 sends whatever
  errors remain back to Gemini for a retry.
- **Caption fixes** (`EditPlan.with_caption_text(3, "…")`) change only text. Segments are
  cached by their inputs, so a re-render skips straight to the final compose (seconds, not
  minutes) and never calls the AI again.

## AI layer (Phase 2)

```
clips ─▶ normalise ─▶ silences · scenes · 360p proxies ─▶ Scribe (only clips with speech; cached)
      ─▶ Gemini: proxies + transcript lines + business context + template ─▶ PlannerOutput JSON
      ─▶ validate · assemble · smart cuts · repair ─(problems? feed back, retry ≤3)─▶ EditPlan
      ─▶ TTS (fit to video length) ─▶ render ─▶ out.mp4 + out.plan.json + out.cost.json
```

Each provider sits behind an interface (`ai/base.py`: `Transcriber`, `Planner`,
`SpeechSynthesizer`). `ai/registry.py` is the only module that names a vendor, so switching
one is a new class plus a config value. Shipped: ElevenLabs Scribe, Gemini (google-genai SDK),
Azure Neural TTS (REST), ElevenLabs TTS, and key-free fakes plus a heuristic planner.

Design decisions:

- **The LLM judges; code does the precise work.** Gemini returns a small `PlannerOutput`
  (`autoedit/schema.py`): which moments in which order, roles, on-screen text, hook and CTA,
  the voiceover script, and *spelling fixes* for transcript lines, referenced by id. It never
  writes caption timings: those come from Scribe's word timestamps, so they are exact, and
  the tokens it would spend echoing timestamps are saved. `autoedit/assemble.py` builds the
  `EditPlan`.
- **Smart cuts are deterministic** (`autoedit/cuts.py`). Speaker segments are tightened to
  word boundaries; pauses over 0.35 s and Amharic hesitation sounds (`እ…`, `ኧ`, `እም`…) are cut
  out. Other segments that keep their sound snap to the nearest word edge, so a cut never
  lands mid-word.
- **Facts come from the business, not the model.** The price comes only from the
  business: an answer, or a number they wrote next to ብር/birr/ETB or "ዋጋ"/"price" in the
  brief. Code extracts it (`autoedit/brief.py`); the LLM never does. A template that needs
  a price refuses to start before any money is spent. The product name and all creative
  text are the LLM's job.
- **The brief drives everything.** `BusinessContext.brief`, the business's own words,
  leads the prompt. Gemini writes the hook, the product name, the call to action, 1–3
  on-screen labels, and picks the colour look. `-t auto` chooses talking head vs product
  promo from how much of the footage is speech.
- **Validation with feedback.** Planner output goes through Pydantic, assembly,
  `repair_plan` and `check_plan`. Remaining errors are sent back in the same conversation
  ("Your JSON had these problems: …"), up to `ADREEL_PLAN_ATTEMPTS`. If every attempt fails,
  or the provider is down, the heuristic planner still produces a video and the run is
  flagged in its warnings.
- **Cheap by construction.**
  - Clips without speech are never transcribed.
  - Transcripts are cached on disk, so re-planning or re-rendering never pays for speech
    recognition twice.
  - Gemini sees 360p / 10 fps proxies at `media_resolution=low`.
  - Media goes inline when small and through the Files API (deleted afterwards) when large.
- **Voiceover.** The script is word-budgeted in the prompt (~2.1 words/s). If the TTS audio
  is still longer than the video, it is re-synthesised faster (up to 1.2× on ElevenLabs,
  1.3× on Azure). The original sound is ducked under it and the voiceover becomes the
  captions. ElevenLabs returns character timestamps, so its captions are exact; Azure's
  REST API returns audio only, so word timings are estimated from the audio's pauses
  (accurate per line, approximate per word).
- **Changing an edit with a prompt** (`AutoEditor.revise`, the 💬 button, `adreel revise`):
  - **What Gemini gets:** the request, the *current* edit (cuts, text, look, voiceover
    script), the clips and the transcript. It returns the complete revised edit, told to
    change only what was asked.
  - **What is kept:** caption fixes the business already made (`carry_fixes`).
    Transcripts come from the cache, so speech recognition is never paid for twice.
  - **Voiceover:** re-recorded only if its script changed.
  - **A new price** is read by code from the request ("ዋጋውን ወደ 400 ብር ቀይር"), never set
    by the model.
  - **Feedback:** the model can return a short `message` when part of a request isn't
    possible ("not enough footage to make it longer"); the bot shows it with the preview.
  - **Failures:** an invalid answer or a provider outage ends with a clear message, and
    the previous preview stays usable.
- **Cost log per run** (`out.cost.json`):
  - billable units for every call: speech-recognition seconds, Gemini input/audio/output
    tokens (thinking included), TTS characters
  - USD and ETB at configurable prices (`ADREEL_PRICES__*`)
  - which planner was used, how many attempts, and timings

  Phase 3 stores these per job in Postgres.

## Templates

Templates are YAML in `src/adreel/templates/` (schema: `src/adreel/schemas/template.py`).
They own layout and pacing; the plan owns content. Colours reference the brand kit by token
(`primary`, `on_primary`, `accent`, `white`, `black`, or `#RRGGBB`), so one template works
for every business. To add a template, drop a YAML file into a directory listed in
`ADREEL_TEMPLATES_DIRS`; no code changes are needed. A template configures:

- duration bounds, and the expected segment roles (used by checks and Phase 2 prompting)
- required slots
- caption style: outline/box, size, position, line width, active-word highlight, timing rules
- on-screen text styles, logo corner/size/opacity, price card and end card
- audio: target LUFS, true peak, ducking
- `reframe.alternate_zoom`: punch-in on consecutive jump cuts
- `ai_guidance` text for the planner LLM

Built-ins: `product_promo` (hook → product shots → price card → CTA, 20–30 s) and
`talking_head` (speaker cuts with alternating 1.12× punch-in, captions, end card, 8–60 s).

## How rendering works

```
clips ─▶ normalise (cached) ─▶ check/repair plan ─▶ cut + reframe each segment (cached)
      ─▶ end-card background ─▶ one .ass overlay ─▶ compose: concat → logo → libass → audio mix
```

- **Normalise**:
  - constant frame rate at the template fps
  - rotation applied, long side ≤ 1920, SAR 1:1
  - HDR (iPhone HLG/PQ) tone-mapped to BT.709
  - two-pass *linear* loudnorm to −14 LUFS
  - a silent track added to clips without audio, so concat never breaks
  - 1 s GOP for fast, frame-accurate trims
- **Timeline**: segment durations are quantised to whole frames and positions accumulated in
  integer frames, so caption times match the video exactly.
- **Reframe** to 9:16, one decision per segment:
  - already 9:16 → scale
  - face found → crop that follows the face
  - landscape with no face → *fit* (the whole frame over a blurred copy of itself, so product
    shots keep the product in frame)
  - anything else → crop at the plan's focus point

  Face tracking runs MediaPipe BlazeFace (full range) on overlapping tiles of frames sampled
  at 3 fps. The whole-frame model misses faces filmed from 1–2 m; the tiles catch them. The
  path is median-filtered and smoothed with a centred moving average (no lag). A still
  subject gets a static crop. A moving one gets its path simplified (Ramer–Douglas–Peucker)
  into a piecewise-linear ffmpeg `crop` expression, so there is no extra decode pass.
- **Colour.** Each clip is measured once with ffmpeg `signalstats`, and the result is
  cached. The correction is mild:
  - dark clips are lifted with gamma, and over-bright ones calmed
  - flat footage gets contrast, but only when the brightened image sits near mid-grey,
    where ffmpeg's `eq` contrast pivots
  - dull colour gets saturation, and garish colour slightly less

  The planner then picks a look: `natural`, `vivid` (food and products), `warm` (cafés) or
  `cool`. Without Gemini, the look comes from the brand's business type. `look: none` in a
  plan turns grading off. Every value is clamped, so a bad measurement can't wreck a clip.
- **Text** is all ASS, rendered by libass with HarfBuzz shaping (captions, hook, label,
  price card, end card, watermark). PlayRes is the 1080×1920 canvas, so preview and final
  have identical layout. **Ethiopic gotcha:** Noto Sans Ethiopic has no Latin letters, ASCII
  digits or `! ? . ,`, and Amharic marketing copy mixes them all the time (`ዋጋ 1,200 ብር!`).
  Every script run is therefore tagged with an explicit font (`\fn`), which gives the same
  result on every host with no fontconfig fallback. Line wrapping and shrink-to-fit use
  glyph widths measured from libass renders, so text never overflows the frame. Colour emoji
  are stripped (libass can't draw them).
- **Audio**: a voiceover is loudness-normalised and the original audio is ducked under it
  with a sidechain compressor (≈15 dB while the VO speaks), then limited to −1 dBFS.
- Every ffmpeg call gets argument lists (no shell), a hard timeout, progress callbacks (for
  the bot's "Processing…" message in Phase 3) and atomic `.part` → final renames.

Bundled assets (see `src/adreel/assets/licenses/`): Noto Sans Ethiopic and Noto Sans (OFL
1.1), and the MediaPipe BlazeFace full-range model (Apache-2.0).

## Configuration

All settings are environment variables prefixed `ADREEL_` (or a `.env` file); see
[`.env.example`](.env.example).

| Variable | Default | Purpose |
|---|---|---|
| `ADREEL_WORK_DIR` | `.work` | Cache of normalised clips and segments (safe to delete) |
| `ADREEL_TEMPLATES_DIRS` | `[]` | Extra template dirs (JSON list); override built-ins by id |
| `ADREEL_FACE_TRACKING` | `true` | Face-aware reframing (needs the `[face]` extra) |
| `ADREEL_WATERMARK_TEXT` | `Made with AdReel` | Free-tier watermark |
| `ADREEL_FFMPEG_BIN` / `ADREEL_FFPROBE_BIN` | `ffmpeg` / `ffprobe` | Binaries |
| `ADREEL_FFMPEG_THREADS` | `0` | Threads per ffmpeg call (0 = auto) |
| `ADREEL_FFMPEG_TIMEOUT_S` | `900` | Hard timeout per ffmpeg call |
| `ADREEL_LOG_LEVEL` / `ADREEL_LOG_JSON` | `INFO` / `false` | Structured logging |
| `ADREEL_TRANSCRIBER` / `ADREEL_PLANNER` | `elevenlabs` / `gemini` | `fake` / `heuristic` need no keys |
| `ADREEL_TTS_STANDARD` / `ADREEL_TTS_PREMIUM` | `azure` / `elevenlabs` | Voiceover providers (or `fake`) |
| `ADREEL_ELEVENLABS_API_KEY`, `_STT_MODEL`, `_TTS_MODEL`, `_VOICE_ID` | –, `scribe_v1`, `eleven_v3`, – | ElevenLabs |
| `ADREEL_GEMINI_API_KEY`, `_MODEL`, `_MEDIA_RESOLUTION`, `_THINKING_BUDGET` | –, `gemini-flash-latest`, `low`, model default | Gemini |
| `ADREEL_AZURE_SPEECH_KEY`, `_REGION`, `_VOICE` | –, `eastus`, `am-ET-MekdesNeural` | Azure TTS |
| `ADREEL_PLAN_ATTEMPTS` | `3` | Planner validate/retry rounds |
| `ADREEL_PRICES__*` | list prices | Unit prices for the cost log (see `.env.example`) |

## Development

```bash
pytest                 # 197 tests, no network; the ffmpeg integration tests skip without ffmpeg
ruff check src tests && ruff format --check src tests
mypy src               # strict
adreel schema -o docs/edit_plan.schema.json   # after changing the schema (a test checks it)
```

Test coverage:

- `test_edit_plan_schema.py`: the shape rules, cross-references, the fix-captions helper,
  and that the JSON Schema stays LLM-friendly and in sync
- `test_caption_timing.py`: mapping through cuts, dropping cut words, splitting on b-roll,
  wrapping, chunking, minimum duration, overlaps and end-card clamping
- `test_reframe.py`: crop maths, filters, keyframe simplification and expression evaluation
- `test_plan_checks_and_brand.py`: checks and repair, Ethiopian phone and handle
  normalisation, contrast
- `test_render_integration.py`: a real preview render, and a re-render after a caption fix
  that must skip the segment stage; also that libass finds every glyph in the bundled fonts
- `test_ai_providers.py`:
  - the cost maths and HTTP retries
  - Scribe and ElevenLabs TTS requests and parsing, and Azure SSML and WAV handling, all
    through `httpx.MockTransport`
  - Gemini requests built with the real google-genai types against a fake client: inline
    vs Files API, retries, usage parsing, and that the schema is accepted
- `test_autoedit.py`: transcript lines, caption fixes, smart cuts, prompt contents,
  assembly (facts from the business, dangling references) and the heuristic planner
- `test_bot.py`: whole conversations through the real aiogram dispatcher, with a mocked
  Telegram session and SQLite:
  - onboarding and brand edits, including logo upload
  - `/new`: clip validation, duplicate clips, resuming a draft, the hourly limit
  - preview actions: fix captions (including Ethiopic numerals), approve, the redo
    limit, double taps, other users' buttons
  - flood control
- `test_jobs.py`: the worker against SQLite, local storage and a fake Telegram:
  - preview, then a caption-fix re-render by a fresh worker that pulls clips from
    storage, then the final as a document
  - a failure the user can fix
  - a transient failure that is retried, then fails cleanly
- `test_phase3_units.py`:
  - parsing: price, colour, phone, handle, caption fixes
  - every message present in both languages with matching placeholders
  - the Bot API client: 429 retry, local-mode files
  - storage path safety, database URLs, the inline queue, RQ enqueue options
- `test_revise.py`: changing an edit with a prompt.
  - Only what was asked changes; caption fixes and the voiceover recording are kept.
  - A new price comes from the business's words.
  - Failures are covered: no AI configured, invalid answers, a provider outage.
  - The worker's `revise` job runs on a fresh worker that pulls clips from storage, and
    the note shows in the preview caption.
- `test_autoedit_integration.py`: real ffmpeg runs with a scripted planner. It returns
  broken JSON, then a too-short plan, then a valid one (three attempts, three cost entries,
  then a render). Also: a provider outage falls back to the heuristic, transcripts are
  cached, and missing facts fail before any spending

## Known limitations

- Cuts only (no transitions or speed ramps) and no background music yet.
- The VO start time is set in the plan; caption timing for VO text arrives with TTS word
  boundaries in Phase 2.
- Face tracking keeps the single most prominent face; two-person framing isn't handled.
- A JPEG logo is composited as a rectangle (no background removal).
- Azure word timings are estimated (see above). Switch the standard tier to the Speech SDK
  if per-word highlight accuracy on standard voices matters.
- Prices in the cost log are configurable list prices, not invoices: check them against
  your plans.
