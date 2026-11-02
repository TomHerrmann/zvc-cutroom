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
| 1. Pilot: harness, brand rules, eval structure, subtitle ingest + review | **done for review**: run on one real video (Breeder, 2026); the live Claude subtitle call is untested (no API key yet) |
| 2. Automated proposals (LLM scoring + drafting, validated quotes) | not started; the four pilot clips were planned by hand |
| 3. Resolve Studio render (scripting API, Smart Reframe, word-highlight captions) | **partly proven**: the pilot timelines were built through the Resolve scripting API by hand; no `cutroom render` command yet |
| 4. Approval queue + metrics feedback into the ranker | not started |
| 5. Portfolio polish: demo video, published eval results | not started |
| Stretch: long-form 4K YouTube edits from raw footage, built with an agent ([plan](docs/longform.md)) | planned |

The only eval set today is `synthetic_smoke`: an invented transcript with one hand-written case
per guardrail (24 cases). It proves the validators work; it says nothing yet about clip quality.
The first real set needs a person's post / never-post labels on the pilot clips.

The pilot source is a finished ZVC review (Breeder, 2026). The raw footage is gone, and this copy
is 1080p at about 1.9 Mbps. A 9:16 crop of it is upscaled, so pilot renders are review copies, not
deliverables.

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
human to check by ear. Words Claude can't verify (an unfamiliar name, a sound-alike like
"B" vs "bee") are never changed automatically: they go into `subtitle_questions.md` with the
sentence before, the sentence, and the sentence after, and stay as transcribed until a person
answers them. The corrected SRT is the YouTube caption file for wide videos.

Vertical clips need more than an SRT. Resolve gives one time range per line, and its scripting
API can't style subtitles or animate them word by word. So `cutroom align` force-aligns the
reviewed text to the audio. This uses pocketsphinx's bundled English model, and it only times
words that are already known; it does no transcription. Captions are then built from those word
times: at most 3 words / 22 characters per line, Montserrat ExtraBold, with the spoken word in
brand blue. An ASS file is written per clip and rendered with ffmpeg/libass to a transparent QuickTime
Animation overlay above the video. Resolve ignored ProRes 4444 alpha in testing; QuickTime
Animation composites correctly. That render step is a shell command today, not a `cutroom` command.

Cuts use the same data. Each clip is an ordered list of source segments, so it can stitch lines
from different parts of the review. When clips are built (`scripts/build_pilot_clips.py` for the
pilot), every segment edge is snapped into measured silence (5 ms audio level, below -55 dB), so
no clip starts or ends inside a word. The validators check this again given the video's silence
map. Whether stitched parts follow one train of thought is still judged by a person. If a longer review exists,
the clip ends on the ZVC title card ("FULL VIDEO ON YOUTUBE"), which slams in over the last
12 frames. Spoken sign-offs like "let me know what you think" are rejected.

## What's here

| module | role |
|---|---|
| `schemas.py` | Pydantic models for transcripts, clip proposals, validation reports, run records. `extra="forbid"` everywhere, so malformed model output fails to parse. |
| `config/brand_rules.yaml` + `config.py` | Brand rules as data: CTA window, cover rules, spoiler terms, hashtags, output formats, subtitle colors, caption style, end card. Hashed into every run record. |
| `validators/` | Pure functions returning typed issues. `quotes` (verbatim match inside the claimed time window: no invented or misattributed quotes), `timestamps` (bounds, length, resolution, caption mode, short-form-is-vertical, overlapping segments, lines cut part-way, cuts mid-speech, YouTube-only content like the title card), `brand` (CTA, cover face + 3-5 words, spoilers, hashtags, caption length), `outro` (below). |
| `align.py` / `audio.py` | Forced alignment of the reviewed words to the audio, and the audio level map that cut edges snap to. |
| `captions.py` | Short caption lines from real word times, written as ASS with the spoken word highlighted. |
| `validators/outro.py` | The end card is required when a longer review exists, spoken sign-offs are rejected, and the card can't point to a review that doesn't exist. |
| `subtitles.py` | Resolve SRT import/export, the Claude accuracy review (structured output), and the checks that gate each correction. |
| `evals.py` | Eval sets of hand-labelled good and bad clips. Reports gate accuracy, good-pass rate, bad-catch rate, and whether bad clips were caught for the right reason. |
| `runlog.py` | Appends a record per command to `runs/runs.jsonl`: latency, tokens, cost, pass/fail counts, failure codes, git sha, brand-rules hash. |
| `store.py` | One JSON file per clip, with review status. |

## Usage

```bash
pip install -e ".[dev,align]"
# In Resolve: generate subtitles from audio, export the subtitle track as SRT
cutroom import-srt episode.srt --id cronov-e01 --duration 3600
cutroom review-subs data/cronov-e01/transcript.resolve.json --glossary names.txt
#   -> subtitles.reviewed.srt (the YouTube caption file) + subtitle_questions.md
ffmpeg -i episode.mp4 -ac 1 -ar 16000 data/cronov-e01/audio16k.wav
cutroom align data/cronov-e01/transcript.reviewed.json --audio data/cronov-e01/audio16k.wav
cutroom validate clips/cronov-e01/ -t data/cronov-e01/transcript.aligned.json \
    --video data/cronov-e01/video.json
cutroom queue --id cronov-e01
cutroom eval evals/sets/synthetic_smoke
cutroom runs
```

`review-subs` calls the Claude API (`ANTHROPIC_API_KEY`); `--review-file` applies a saved review
without a call. `validate` runs the audio-based cut checks and the end-card rules only when
`--video` is given.

## Development

```bash
pytest -q && ruff check . && ruff format --check .
```

CI runs the tests and every eval set on each push. An eval regression fails the build.

## License

MIT
