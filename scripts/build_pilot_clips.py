"""Build the Breeder pilot clip proposals from transcript lines, snapping every cut into
measured silence. Output: clips/breeder-2026-review/*.json (the edit decisions Resolve renders)."""

import sys
from pathlib import Path

from cutroom.audio import Level
from cutroom.config import load_rules
from cutroom.render import export_plan
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
from cutroom.validators import validate_clip

D = Path("data/breeder-2026-review")
# Line ids come from the reviewed SRT; word times from the forced alignment (cutroom align).
srt = Transcript.model_validate_json((D / "transcript.reviewed.json").read_text())
t = Transcript.model_validate_json((D / "transcript.aligned.json").read_text())
v = VideoContext.model_validate_json((D / "video.json").read_text())
cues = t.segments
words = t.words()
level = Level.from_wav(Path(sys.argv[2]) if len(sys.argv) > 2 else D / "audio16k.wav")


def seg(first: float, last_end: float):
    """first/last_end: SRT times of the first and last line to keep. Cuts land in the gap
    between real words: never inside the first or last word, never on a neighbour."""
    ids = [s.id for s in srt.segments if s.start >= first - 0.05 and s.end <= last_end + 0.05]
    lines = [c for c in cues if c.id in ids and c.words]
    w0, w1 = lines[0].words[0], lines[-1].words[-1]
    i0, i1 = words.index(w0), words.index(w1)
    prev_end = words[i0 - 1].end if i0 > 0 else 0.0
    next_start = words[i1 + 1].start if i1 + 1 < len(words) else t.duration
    start, ok_in = level.cut_in(w0.start, prev_end)
    end, ok_out = level.cut_out(w1.end, next_start)
    if not (ok_in and ok_out):
        edge = "start" if not ok_in else "end"
        print(f"  no clean pause at the {edge} of {first:.2f}-{last_end:.2f}")
    for a, b in v.youtube_only:  # never bleed into long-form-only material (title card)
        if start < a < end:
            end = a - 1 / 30
        if start < b < end:
            start = b + 1 / 30
    return ClipSegment(start=round(max(start, 0.0), 3), end=round(end, 3))


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
    # One thread per clip. No spoken sign-off: the ZVC end card points to the full review.
    (
        "v1-verdict",
        "Breeder: a great pitch, a middle-of-the-road thriller",
        [(0.125, 8.167), (333.625, 348.125), (42.708, 47.625)],
        "Post-Roe horror with a lot to say and not much tension. Full review on YouTube.",
        ["#zerovisioncinema", "#breeder", "#horror", "#moviereview"],
    ),
    (
        "v2-gender-swap",
        "Breeder flips who gets exposed on screen",
        [(258.25, 270.917), (280.167, 307.792)],
        "Breeder puts the man on display, and that's the point.",
        ["#zerovisioncinema", "#breeder", "#horror", "#filmanalysis"],
    ),
    (
        "v3-who-decides",
        "Who gets to decide who breeds?",
        [(186.167, 200.042), (316.25, 333.417)],
        "Breeder and The Handmaid's Tale ask the same question.",
        ["#zerovisioncinema", "#breeder", "#horror", "#handmaidstale"],
    ),
    (
        "v4-no-thrills",
        "A thriller without the thrills",
        [(28.208, 42.5), (348.208, 355.917)],
        "A thriller has one job. Full review on YouTube.",
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
        end_card=rules.end_card.topic_title,
        created_by="human",
    )
    rep = validate_clip(clip, t, rules, video=v)
    store.put(clip)
    print(f"{cid}: {clip.duration:.1f}s, {len(segments)} segments, errors={rep.error_codes}")
    for g in segments:
        print(f"    {g.start:8.3f} -> {g.end:8.3f}")

# Resolve hand-off: plan.json + per-clip SRT and caption files (same as `cutroom export`).
out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data") / t.video_id / "resolve"
p = export_plan([store.get(t.video_id, cid) for cid, *_ in PLAN], t, rules, out_dir)
print(f"wrote {p}")
