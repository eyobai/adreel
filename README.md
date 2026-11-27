# AdReel

Raw phone clips in → a branded, ready-to-post 9:16 TikTok/Instagram video out, with burned-in
Amharic captions, price card, logo and call-to-action end card. Built for small businesses in
Ethiopia and delivered as a Telegram bot.

| Phase | Scope | Status |
|---|---|---|
| 1 | Render engine + CLI (clips + edit plan + brand kit → video) | **done** |
| 2 | AI layer: Scribe transcription, Gemini edit plan, Azure/ElevenLabs TTS, cost log | next |
| 3 | Telegram bot (aiogram 3), Redis/RQ worker, Postgres, preview/approve/fix-captions | |
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
| `adreel render -p PLAN -b BRAND -c ID=FILE… -o OUT [-q preview\|final] [--watermark] [--voiceover WAV] [--no-repair] [--no-face]` | Render a video |
| `adreel validate -p PLAN [-c ID=FILE…] [--repair]` | Schema check; with clips, also checks durations and the template, and can print the auto-repaired plan |
| `adreel schema [-o FILE]` | Dump the edit-plan JSON Schema (the contract Gemini fills in Phase 2) |
| `adreel templates` | List templates |
| `adreel samples [-o DIR]` | Generate synthetic test media |

`-q preview` renders 540×960 at ~1 Mbit/s (a 30 s preview is ~2–4 MB, for slow mobile
connections). `-q final` renders 1080×1920 at CRF 19 (sent as a document so Telegram does not
recompress it).

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

## Development

```bash
pytest                 # 94 tests; the ffmpeg integration tests skip if ffmpeg is missing
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

## Known limitations (Phase 1)

- Cuts only (no transitions or speed ramps) and no background music yet.
- The VO start time is set in the plan; caption timing for VO text arrives with TTS word
  boundaries in Phase 2.
- Face tracking keeps the single most prominent face; two-person framing isn't handled.
- A JPEG logo is composited as a rectangle (no background removal).
