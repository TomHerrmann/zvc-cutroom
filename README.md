# cutroom

A human-in-the-loop pipeline that turns long [Zero Vision Cinema](https://github.com/TomHerrmann/zero-vision-cinema)
videos into short vertical clips. It finds the strongest moments, drafts captions and covers,
renders in DaVinci Resolve Studio, and queues everything for a person to approve.
**Nothing posts automatically.**

The interesting part is the harness around the model: brand rules as config, typed outputs,
validators that reject bad clips before a human ever sees them, an eval set that runs on every
change, and a log of cost, latency and pass rates for every run.

## Status

| milestone | state |
|---|---|
| 1. Pilot: harness, brand rules, eval structure, subtitle ingest + review | **in progress**: harness done, not yet run on a real recording |
| 2. Automated proposals (LLM scoring + drafting, validated quotes) | not started |
| 3. Resolve Studio render (scripting API, Smart Reframe, styled subtitles) | not started |
| 4. Approval queue + metrics feedback into the ranker | not started |
| 5. Portfolio polish: demo video, published eval results | not started |

The only eval set today is `synthetic_smoke`: an invented transcript with one hand-written case
per guardrail. It proves the validators work; it says nothing yet about clip quality.

## Pipeline

```
video ─► Subtitles ─► Understand ─► Generate ─► Guardrails ─► Render ─► Review ─► Publish ─► Measure
         Resolve AI    moment         cuts,       validators    Resolve    human      approved   views/follows
         subtitles,    scoring        caption,    (reject or    scripting  approves   only       feed the
         Claude        (hook, face,   hashtags,   pass)         + Smart    / edits /             ranker
         accuracy pass quote, theme)  titles,                   Reframe    rejects
                                      covers
```

One firm rule: **clip decisions live as data in this repo's clip store and are exported to
Resolve.** Resolve is the renderer, not the database.

### Output formats

| format | form | resolution | platform | captions |
|---|---|---|---|---|
| vertical | short | 1080x1920 | TikTok | burned in: brand off-white `#FFFDF6`, active word highlighted in brand blue `#4A8CC6` |
| wide | long | 3840x2160 | YouTube | plain SRT with timestamps, uploaded alongside |

Every clip proposal is short form, so every clip proposal is vertical. The validators enforce it.

### Subtitles

Subtitles come from DaVinci Resolve Studio's AI transcription. The exported SRT gets an accuracy
pass from Claude (misheard words, names, film titles), returned as a typed list of corrections,
never a rewritten file. Each correction must quote its cue exactly and can't touch timing;
anything that doesn't line up with a real cue is rejected, and heavy rewrites are flagged for a
human to check by ear. The corrected SRT goes back onto Resolve's subtitle track.

## What's here

| module | role |
|---|---|
| `schemas.py` | Pydantic models for transcripts, clip proposals, validation reports, run records. `extra="forbid"` everywhere, so malformed model output fails to parse. |
| `config/brand_rules.yaml` + `config.py` | Brand rules as data: CTA window, cover rules, spoiler terms, hashtags, output formats, subtitle colors. Hashed into every run record. |
| `validators/` | Pure functions returning typed issues. `quotes` (verbatim match inside the claimed time window: no invented or misattributed quotes), `timestamps` (bounds, length, resolution, caption mode, short-form-is-vertical, mid-word cuts), `brand` (CTA, cover face + 3-5 words, spoilers, hashtags, caption length). |
| `subtitles.py` | Resolve SRT import/export, the Claude accuracy review (structured output), and the checks that gate each correction. |
| `evals.py` | Eval sets of hand-labelled good and bad clips. Reports gate accuracy, good-pass rate, bad-catch rate, and whether bad clips were caught for the right reason. |
| `runlog.py` | Appends a record per command to `runs/runs.jsonl`: latency, tokens, cost, pass/fail counts, failure codes, git sha, brand-rules hash. |
| `store.py` | One JSON file per clip, with review status. |

## Usage

```bash
pip install -e ".[dev]"
# In Resolve: generate subtitles from audio, export the subtitle track as SRT
cutroom import-srt episode.srt --id cronov-e01 --duration 3600
cutroom review-subs data/cronov-e01/transcript.resolve.json --glossary names.txt
#   -> data/cronov-e01/subtitles.reviewed.srt, ready to re-import into Resolve
cutroom validate my_clips/ -t data/cronov-e01/transcript.reviewed.json
cutroom eval evals/sets/synthetic_smoke
cutroom runs
```

`review-subs` calls the Claude API (`ANTHROPIC_API_KEY`); `--review-file` applies a saved review
without a call.

## Development

```bash
pytest -q && ruff check . && ruff format --check .
```

CI runs the tests and every eval set on each push. An eval regression fails the build.

## License

MIT
