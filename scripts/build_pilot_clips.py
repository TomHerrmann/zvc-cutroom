"""Build the Breeder pilot clip proposals from transcript lines, snapping every cut into
measured silence. Output: clips/breeder-2026-review/*.json (the edit decisions Resolve renders)."""

import json
import sys
from pathlib import Path

from cutroom.config import load_rules
from cutroom.schemas import (
    CTA,
    ClipProposal,
    ClipSegment,
    CoverCandidate,
    QuoteRef,
    Transcript,
    VideoContext,
)
from cutroom.store import ClipStore
from cutroom.subtitles import clip_transcript, write_srt
from cutroom.validators import validate_clip

D = Path("data/breeder-2026-review")
t = Transcript.model_validate_json((D / "transcript.reviewed.json").read_text())
v = VideoContext.model_validate_json((D / "video.json").read_text())
cues = t.segments


def cue_at(time: float):
    return min(cues, key=lambda s: abs(s.start - time))


def cut_in(t0: float) -> float:
    """Start inside the pause before the first word."""
    cands = [(a, b) for a, b in v.silences if a <= t0 + 0.05 and b >= t0 - 0.6]
    if not cands:
        return max(t0 - 0.1, 0.0)
    a, b = cands[-1]
    return round(max(a + 0.02, min(t0 - 0.1, b - 0.05)) if b > t0 else max(a + 0.02, b - 0.15), 3)


def cut_out(t1: float) -> float:
    """End inside the pause after the last word."""
    cands = [(a, b) for a, b in v.silences if b >= t1 and a <= t1 + 0.6]
    if not cands:
        return round(min(t1 + 0.3, t.duration), 3)
    a, b = cands[0]
    return round(min(t1 + 0.15, b - 0.02) if a <= t1 else min(a + 0.2, (a + b) / 2), 3)


def seg(first: float, last_end: float):
    """first = start time of the first cue, last_end = end time of the last cue."""
    start, end = cut_in(first), cut_out(last_end)
    prev_end = max((s.end for s in cues if s.end <= first + 0.01), default=0.0)
    next_start = min((s.start for s in cues if s.start >= last_end - 0.01), default=t.duration)
    start = max(start, prev_end + 0.02)  # never pick up the tail of the previous line
    end = min(end, next_start - 0.02)  # never pick up the start of the next line
    for a, b in v.youtube_only:  # never bleed into long-form-only material (title card)
        if start < a < end:
            end = round(a - 1 / 30, 3)
        if start < b < end:
            start = round(b + 1 / 30, 3)
    return ClipSegment(start=start, end=end)


def quotes_for(segments):
    out = []
    for g in segments:
        lines = [s for s in cues if s.start >= g.start - 0.05 and s.end <= g.end + 0.05]
        if lines:
            out.append(
                QuoteRef(
                    text=" ".join(s.text for s in lines), start=lines[0].start, end=lines[-1].end
                )
            )
    return out


PLAN = [
    (
        "v1-verdict",
        "Breeder: a great pitch, a middle-of-the-road thriller",
        [(0.125, 8.167), (42.708, 47.625), (357.167, 361.5), (483.5, 484.375)],
        "Post-Roe horror with a lot to say and not much tension. Full review on the channel.",
        ["#zerovisioncinema", "#breeder", "#horror", "#moviereview"],
    ),
    (
        "v2-gender-swap",
        "Breeder flips who gets exposed on screen",
        [(283.75, 307.792), (483.5, 484.375)],
        "Breeder puts the man on display, and that's the point.",
        ["#zerovisioncinema", "#breeder", "#horror", "#filmanalysis"],
    ),
    (
        "v3-who-decides",
        "Who gets to decide who breeds?",
        [(195.875, 200.042), (316.25, 329.417)],
        "Breeder and The Handmaid's Tale ask the same question.",
        ["#zerovisioncinema", "#breeder", "#horror", "#handmaidstale"],
    ),
    (
        "v4-no-thrills",
        "A thriller without the thrills",
        [(28.208, 37.542), (383.333, 389.667), (484.542, 485.875), (489.0, 490.208)],
        "Big ideas, no narrative drive. Am I way off base?",
        ["#zerovisioncinema", "#breeder", "#horror", "#moviereview"],
    ),
]

rules = load_rules()
store = ClipStore("clips")
ok = True
for cid, hook, ranges, caption, tags in PLAN:
    segments = [seg(a, b) for a, b in ranges]
    clip = ClipProposal(
        id=cid,
        video_id=t.video_id,
        segments=segments,
        hook=hook,
        caption=caption,
        hashtags=tags,
        title_variants=[hook],
        quotes=quotes_for(segments),
        cta=CTA(text="Follow Zero Vision Cinema", kind="overlay", at=1.0),
        covers=[
            CoverCandidate(
                frame_time=round(segments[0].start + 1.5, 2),
                overlay_text=" ".join(hook.split()[:5]),
                selected=True,
            )
        ],
        created_by="human",
    )
    rep = validate_clip(clip, t, rules, video=v)
    store.put(clip)
    print(f"{cid}: {clip.duration:.1f}s, {len(segments)} segments, errors={rep.error_codes}")
    for g in segments:
        print(f"    {g.start:8.3f} -> {g.end:8.3f}")

# Resolve hand-off: segment list + per-clip SRT re-timed onto the clip timeline.
out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data") / t.video_id / "resolve"
out_dir.mkdir(parents=True, exist_ok=True)
plan = []
for cid, *_ in PLAN:
    clip = store.get(t.video_id, cid)
    write_srt(clip_transcript(t, clip), out_dir / f"{cid}.srt")
    plan.append(
        {
            "id": cid,
            "segments": [[round(g.start, 3), round(g.end, 3)] for g in clip.segments],
            "srt": f"{cid}.srt",
        }
    )
(out_dir / "plan.json").write_text(json.dumps(plan, indent=1))
print(f"wrote {out_dir}/plan.json")
