# Stretch phase: long-form 4K YouTube edits from raw footage

**Status: first episode shipped** (16mm Journey ep1, Nov 2026). Pacing, the Resolve edit, music and grade fixes are commands. The finishing steps, and what ep1 taught, are in [finishing.md](finishing.md). The plan below is the original draft.

## What it is

The clip pipeline runs one finished long video down into short vertical clips. This phase runs
the other way: drop in one or more *raw* videos and work with an agent, through CLI commands,
to build a polished long-form cut for YouTube. That means 3840x2160, 16:9, with a title card,
color grade, on-screen CTA callouts and name lower-thirds.

It reuses the harness rather than starting over: the same brand rules, typed outputs,
validators, evals and run log, and the same rule that **the edit lives as data in this repo
and Resolve renders it**.

## How it fits the existing design

| piece | clip pipeline (milestones 1-5) | long-form phase |
|---|---|---|
| input | one finished long video | one or more raw source files |
| transcript | Resolve AI subtitles + Claude review | same, run per source file |
| decision object | `ClipProposal` (one in/out range) | `EditPlan`: ordered segments across sources, plus title card, lower-thirds, CTA callouts, grade |
| format | vertical, 1080x1920, burned-in captions | wide, 3840x2160, SRT sidecar (already in `brand_rules.yaml`) |
| human loop | approve, edit or reject each clip | iterative: the agent proposes a cut, you give notes, it revises the plan |
| render | Resolve scripting + Smart Reframe | Resolve scripting: multi-source timeline, grade, Fusion titles |

## Draft data model

- `SourceMedia`: id, file path, duration, timecode start, per-source reviewed transcript.
- `Segment`: source id, in, out, purpose (`intro`, `body`, `b-roll`, `outro`), notes.
- `LowerThird`: name, role, at, duration. Names must come from a people roster in config.
- `CTACallout`: text, at, style. Placement rules live in brand config, same as clips.
- `TitleCard`: title, subtitle, duration, template id.
- `GradeSpec`: a named look from config (a LUT or a saved Resolve grade), applied per source.
- `EditPlan`: ordered segments + overlays + grade + version history, so every revision is diffable.

## Draft CLI loop

```bash
cutroom longform new --id cronov-2026-ep03
cutroom longform add-source raw/a-cam.mov raw/b-cam.mov
cutroom longform plan                       # agent drafts an EditPlan from the transcripts
cutroom longform revise "tighten the intro, lose the tangent about lenses"
cutroom longform validate                   # same idea as clip validators
cutroom longform render --draft             # fast proxy render for review
cutroom longform render --final             # 4K master + YouTube SRT
```

## Guardrails to add

- Every segment points at real source time within that file's duration; no overlapping
  segments unless marked as a cutaway.
- Any spoken line the plan quotes, such as a chapter title built from a quote, must be
  verbatim in that source's transcript.
- Lower-third names must be in the roster, spelled exactly.
- CTA callouts follow the brand rules (timing, count, wording).
- Final runtime falls inside a configured range; title card present; grade named in config.
- Output is 3840x2160 with the YouTube preset and an SRT built from the reviewed subtitles.

## Evals

Hand-edit a few episodes yourself first, then score the agent's plan against your cut:
how much of your kept footage it kept (and vice versa), how close its cut points are to
yours, and how many guardrail violations it produced. Track how many revision rounds each
cut needed, which should fall over time.

## Open questions

- Which Resolve grade assets to standardize on (LUTs vs saved grades), and whether the grade
  should vary per camera.
- Title card and lower-third templates: build them as Fusion templates once, so the script only
  fills in text.
- Multicam: whether A/B camera sync happens in Resolve by hand before the agent starts.
