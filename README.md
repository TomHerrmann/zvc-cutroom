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
| 2. Automated proposals (LLM scoring + drafting, validated quotes) | **in review**: `cutroom propose`, run from saved drafts on Breeder (6 of 6 pass, 3 of the 4 hand-picked pilot clips recovered) and on raw vlog footage (16mm Journey ep1, 6 of 6 pass). The live Claude call is tested against a fake client only (no API key yet) |
| 3. Resolve Studio render (scripting API, Smart Reframe, word-highlight captions) | **in review**: `export`, `captions` and `resolve-build` commands, tested against a fake Resolve in CI, and run for real in Resolve Studio 21 on the six 16mm Journey ep1 clips (Smart Reframe, Acid TM word-highlight captions, end card, H.264 render) |
| 4. Approval queue + metrics feedback into the ranker | not started |
| 5. Retrieval across the whole channel (RAG over every transcript, below) | not started |
| 6. Portfolio polish: demo video, published eval results | not started |
| Stretch: long-form 4K YouTube edits from raw footage, built with an agent ([plan](docs/longform.md)) | **first piece in review**: `cutroom pace` (talking kept, unspoken work sped up by kind, dead air cut), run on 16mm Journey ep1 from a saved draft |

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
times: at most 3 words / 22 characters per line, in the brand font Acid TM, with the spoken word in
brand blue. An ASS file is written per clip and rendered with ffmpeg/libass to a transparent QuickTime
Animation overlay above the video. Resolve ignored ProRes 4444 alpha in testing; QuickTime
Animation composites correctly. `cutroom captions` renders the overlays (it needs ffmpeg built with libass).

Cuts use the same data. Each clip is an ordered list of source segments, so it can stitch lines
from different parts of the review. When clips are built (`scripts/build_pilot_clips.py` for the
pilot), every segment edge is snapped into measured silence (5 ms audio level, below -55 dB), so
no clip starts or ends inside a word. The validators check this again given the video's silence
map. Whether stitched parts follow one train of thought is still judged by a person. If a longer review exists,
the clip ends on the ZVC title card ("FULL VIDEO ON YOUTUBE"), which slams in over the last
12 frames. Spoken sign-offs like "let me know what you think" are rejected.

### Automated proposals (milestone 2)

Claude never writes timestamps. It sees the reviewed subtitle lines numbered `[id] start-end
text` and answers with line-id ranges, so every cut starts and ends on a whole line, and code
places the edge in measured silence. Lines are marked in the prompt when they can't be used:
`LONG-FORM ONLY` (the title card), `SIGN-OFF` (replaced by the end card), and `NO PAUSE AFTER`
(the speaker runs straight into the next line, so a cut there would land mid-word; this is
computed with the same cut placement the validator checks). Quotes carry the line ids they come
from; the quote validator then requires the exact words inside those lines.

What the model can't see is measured, not asked for: `face_on_screen` and the cover's face come
from the video frames, `length_fit` from the clip length against `proposals.target_seconds`.
Clips are ranked by a weighted mean of the scores (`proposals.weights` in the brand rules).

A clip that fails validation goes back to Claude once with the failing codes and messages
(`proposals.max_repairs`). Anything still failing is stored as `rejected_by_validator`, so the
reviewer can see what was tried; nothing is approved automatically. Re-running replaces earlier
machine proposals for that video but never touches one a person has judged.

When hand-picked clips exist for the video, `propose` reports which machine clip covers each
one (overlap of source time) and the share covered, which is the first ranking metric.

On Breeder, from a draft written in the model's output format (no API key yet), the first draft
had two clips rejected for `CUT_MID_SPEECH`: both ended or started where the speaker doesn't
pause. That is what led to the `NO PAUSE AFTER` marks. With them, 6 of 6 proposals pass. The
top clip matches the hand-built "who decides" clip exactly, "gender swap" overlaps the hand
pick by 86%, and the verdict clip isn't recovered (the hand pick stitches three ranges the
draft didn't choose).

The second test was a different format: episode 1 of 16mm Journey, 18 minutes of raw, unedited
phone footage (a vlog, with the intro recorded seven times). It found three things a finished
review never hit, all fixed here: numbers like "16mm" had no pronunciation and crashed the
aligner; the projector's motor noise sat above the fixed -55 dB silence level, so pauses are
now measured against the local noise floor when there is one (gated audio is unchanged); and
over long stretches with no talking the aligner spread a line's words across a minute, so a
line whose words span far more than its subtitle cue keeps its estimated times. The face check
also mattered here: he often has his back to the camera, so when the cover frame Claude picks
has no face, the nearest frame in the clip that does is used. All six proposals pass, with
`--about` telling the model it's a vlog and to use one take of the intro.

### Long-form pacing (first piece of the long-form phase)

Raw footage has long stretches with no talking. `cutroom pace` turns one raw take into an
`EditPlan`: source ranges, each with a playback speed. Spoken lines follow the clip rules:
whole lines only, one take of anything recorded twice, edges in measured silence, always real
time. Every unspoken gap of 2 s or more between two kept lines is a stretch, and Claude labels
each one from a contact sheet of its frames (stamped with source times), splitting it where
the action changes:

| kind | what it is | first-round speed |
|---|---|---|
| idle | standing, thinking, gearing up to talk | cut |
| empty | nobody in frame | cut |
| setup | big moves: unboxing, carrying, plugging in | 4x |
| handwork | hands on the projector or a reel | 4x |
| repeat | the same motion over and over: spooling, rewinding | 8x |
| show | worth seeing at real time: the first image on the screen | 1x |

Claude only labels; code picks the speed (`longform.pacing` in the brand rules). A stretch
under 5 s is a beat between lines and never goes past 2x, a long one steps up the levels
(1, 2, 4, 8) until it plays in 15 s or less, and any part of a work stretch where the frame is
measured still for 2 s is cut. Anything left unlabelled is cut and reported. `check_plan`
then rejects a plan that speeds up or clips a spoken word. Sped parts keep their sound,
pitch-corrected and at half volume. Outputs: `edit_plan.json`, `cut.srt` (the kept lines on
the cut's timeline, for YouTube) and `render.sh` (an ffmpeg review render, one file per
segment, resumable in pieces). `cutroom resolve-edit` builds the same plan as a Resolve
timeline: real speed changes on the clips (pitch-corrected), sped pieces coloured by speed,
and the SRT on a subtitle track, read back and checked against the plan.

On 16mm Journey ep1, 17.9 minutes of source became a 7.6 minute cut: 6.1 minutes of talk and
1.4 minutes of sped-up work standing in for 6.6 minutes of it, plus 7 seconds shown at real time. A frame-difference motion
measure alone couldn't tell "standing still talking himself up" from slow hand work (both
change about 4% of pixels), and OpenCV's face detector fired on posters and a cushion, which
is why the kind comes from Claude looking at the frames rather than from a threshold.

## What's here

| module | role |
|---|---|
| `schemas.py` | Pydantic models for transcripts, clip proposals, validation reports, run records. `extra="forbid"` everywhere, so malformed model output fails to parse. |
| `config/brand_rules.yaml` + `config.py` | Brand rules as data: CTA window, cover rules, spoiler terms, hashtags, output formats, subtitle colors, caption style, end card. Hashed into every run record. |
| `validators/` | Pure functions returning typed issues. `quotes` (verbatim match inside the claimed time window: no invented or misattributed quotes), `timestamps` (bounds, length, resolution, caption mode, short-form-is-vertical, overlapping segments, lines cut part-way, cuts mid-speech, YouTube-only content like the title card), `brand` (CTA, cover face + 3-5 words, spoilers, hashtags, caption length), `outro` (below). |
| `align.py` / `audio.py` | Forced alignment of the reviewed words to the audio, and the audio level map that cut edges snap to. |
| `captions.py` | Short caption lines from real word times, written as ASS with the spoken word highlighted. |
| `validators/outro.py` | The end card is required when a longer review exists, spoken sign-offs are rejected, and the card can't point to a review that doesn't exist. |
| `propose.py` | Milestone 2. Claude reads the numbered transcript lines and returns a typed `ProposalDraft`: per moment, line ranges to play, hook, caption, hashtags, titles, cover text, verbatim quotes, and 0-1 scores for hook, quote punch and theme. Code turns that into a `ClipProposal`, the validators gate it, failures go back to Claude once with the exact issues, and passing clips are ranked and queued for review. |
| `pacing.py` | Long-form pacing: spoken runs, unspoken stretches, the `PacingDraft` Claude returns (keep ranges + labelled stretches with contact sheets), the `EditPlan` with per-segment speeds, its checks, the retimed SRT and the ffmpeg render script. |
| `cuts.py` | Line ranges to segments, edges snapped into measured silence. Shared by the proposer and the pilot script. |
| `faces.py` | OpenCV face check on the source video: the cover frame's face, and `face_on_screen` (share of sampled frames with a face). `faces` extra. |
| `subtitles.py` | Resolve SRT import/export, the Claude accuracy review (structured output), and the checks that gate each correction. |
| `evals.py` | Eval sets of hand-labelled good and bad clips. Reports gate accuracy, good-pass rate, bad-catch rate, and whether bad clips were caught for the right reason. |
| `runlog.py` | Appends a record per command to `runs/runs.jsonl`: latency, tokens, cost, pass/fail counts, failure codes, git sha, brand-rules hash. |
| `store.py` | One JSON file per clip, with review status. |
| `video.py` | Audio extraction, silence map and scene cuts for a source video (`video.json`). |
| `render.py` | Exports approved vertical clips to `plan.json` + SRT + ASS, and renders the caption overlays. |
| `resolve_build.py` | Builds one 1080x1920 timeline per clip through the Resolve scripting API: segments on V1 (Smart Reframed), captions on V2, end card on V3 over the last 12 frames. Reads each timeline back and checks it against the plan. |

## Usage

```bash
pip install -e ".[dev,align,faces]"
# In Resolve: generate subtitles from audio, export the subtitle track as SRT
cutroom import-srt episode.srt --id cronov-e01 --duration 3600
cutroom review-subs data/cronov-e01/transcript.resolve.json --glossary names.txt
#   -> subtitles.reviewed.srt (the YouTube caption file) + subtitle_questions.md
ffmpeg -i episode.mp4 -ac 1 -ar 16000 data/cronov-e01/audio16k.wav
cutroom align data/cronov-e01/transcript.reviewed.json --audio data/cronov-e01/audio16k.wav
cutroom propose data/cronov-e01/transcript.aligned.json --video data/cronov-e01/video.json \
    --audio data/cronov-e01/audio16k.wav --source episode.mp4 --about "a vlog, not a review"
cutroom pace data/cronov-e01/transcript.aligned.json --audio data/cronov-e01/audio16k.wav \
    --frames proxy.mp4 --out ~/edits/cronov-e01 --render-source episode.mp4
cutroom resolve-edit ~/edits/cronov-e01/edit_plan.json --source episode.mp4 \
    --name "cronov-e01 v1" --srt ~/edits/cronov-e01/cut.srt
cutroom validate clips/cronov-e01/ -t data/cronov-e01/transcript.aligned.json \
    --video data/cronov-e01/video.json
cutroom queue --id cronov-e01
cutroom export --id cronov-e01 -t data/cronov-e01/transcript.aligned.json --out ~/resolve/cronov-e01
cutroom captions ~/resolve/cronov-e01/plan.json --fonts fonts
cutroom resolve-build ~/resolve/cronov-e01/plan.json --source episode.mp4 --project cutroom
cutroom review cronov-e01 v1-verdict post --note "clean argument, strong hook"
cutroom eval evals/sets/synthetic_smoke
cutroom runs
```

`review-subs`, `propose` and `pace` call the Claude API (`ANTHROPIC_API_KEY`); `--review-file` and
`--draft-file` replay a saved result without a call. `validate` runs the audio-based cut checks and the end-card rules only when
`--video` is given.

## To do

- **Social post copy per clip.** Alongside each rendered clip, draft the post description from
  what is actually said in the clip: a hook line, a CTA, hashtags, and per-platform mentions of
  the film, distributor, production studio and director. Platforms without clickable links get a
  "link in bio" note; YouTube Shorts gets the real link to the full review. Typed output,
  validated like the clips (every claim traceable to the transcript, handles checked against a
  list, platform length limits).
- **Buffer drafts.** Push that copy to Buffer as drafts only, one per connected channel, for a
  person to finish and schedule. Never `shareNow` or queue. Video posts need a public video URL,
  so the rendered clip has to be hosted first.
- **Retrieval across every video (RAG).** Chunk and embed the reviewed transcripts of every
  ZVC video, kept in Postgres with pgvector, so that clip picking and social copy can draw on
  the whole channel, not one video at a time: finding the strongest moments on a theme,
  pulling earlier takes on the same director or studio, and not repeating a clip already
  posted. Retrieval quality is measured, not assumed: a labelled query set (query to the
  moments a person says are right) scored by recall@k and MRR in the eval run, so a change
  to chunking or the embedding model shows up as a number.
- Record post / never verdicts on the pilot clips and build the first real eval set.

## Development

```bash
pytest -q && ruff check . && ruff format --check .
```

CI runs the tests and every eval set on each push. An eval regression fails the build.

## License

MIT
