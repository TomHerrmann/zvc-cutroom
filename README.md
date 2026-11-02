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
| 1. Pilot: harness, brand rules, eval structure, transcription | **in progress**: harness done, real recording not yet transcribed |
| 2. Automated proposals (LLM scoring + drafting, validated quotes) | not started |
| 3. Resolve Studio render (scripting API, Smart Reframe, ffmpeg drafts) | not started |
| 4. Approval queue + metrics feedback into the ranker | not started |
| 5. Portfolio polish: demo video, published eval results | not started |

The only eval set today is `synthetic_smoke`: an invented transcript with one hand-written case
per guardrail. It proves the validators work; it says nothing yet about clip quality.

## Pipeline

```
video ─► Ingest ─► Understand ─► Generate ─► Guardrails ─► Render ─► Review ─► Publish ─► Measure
         whisper    moment         cuts,       validators    Resolve    human      approved   views/follows
         + Resolve  scoring        caption,    (reject or    scripting  approves   only       feed the
         transcript (hook, face,   hashtags,   pass)         + Smart    / edits /             ranker
         cross-check quote, theme) titles,                   Reframe    rejects
                                   covers
```

One firm rule: **clip decisions live as data in this repo's clip store and are exported to
Resolve.** Resolve is the renderer, not the database.

## What's here

| module | role |
|---|---|
| `schemas.py` | Pydantic models for transcripts, clip proposals, validation reports, run records. `extra="forbid"` everywhere, so malformed model output fails to parse. |
| `config/brand_rules.yaml` + `config.py` | Brand rules as data: CTA window, cover rules, spoiler terms, hashtag rules, per-platform length and format. Hashed into every run record. |
| `validators/` | Pure functions returning typed issues. `quotes` (exact normalised match inside the claimed time window, so no invented or misattributed quotes), `timestamps` (bounds, platform length, aspect ratio, mid-word cuts), `brand` (CTA, cover face + 3-5 words, spoilers, hashtags, caption length). |
| `ingest.py` | ffmpeg audio extraction, faster-whisper transcription with word timestamps, Resolve SRT import, and a Whisper-vs-Resolve cross-check (WER + disagreement regions). |
| `evals.py` | Eval sets of hand-labelled good and bad clips. Reports gate accuracy, good-pass rate, bad-catch rate, and whether bad clips were caught for the right reason. |
| `runlog.py` | Appends a record per command to `runs/runs.jsonl`: latency, tokens, cost, pass/fail counts, failure codes, git sha, brand-rules hash. |
| `store.py` | One JSON file per clip, with review status. |

## Usage

```bash
pip install -e ".[dev]"          # add ,asr for Whisper
cutroom transcribe episode.mp4 --id cronov-e01
cutroom import-srt resolve_export.srt --id cronov-e01 --duration 3600
cutroom crosscheck data/cronov-e01/transcript.whisper.json data/cronov-e01/transcript.resolve.json
cutroom validate my_clips/ -t data/cronov-e01/transcript.whisper.json
cutroom eval evals/sets/synthetic_smoke
cutroom runs
```

## Development

```bash
pytest -q && ruff check . && ruff format --check .
```

CI runs the tests and every eval set on each push. An eval regression fails the build.

## License

MIT
