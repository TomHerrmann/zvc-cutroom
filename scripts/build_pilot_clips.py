"""Build the Breeder pilot clip proposals from transcript lines, snapping every cut into
measured silence. Output: clips/breeder-2026-review/*.json (the edit decisions Resolve renders)."""

import sys
from pathlib import Path

from cutroom.audio import Level
from cutroom.config import load_rules
from cutroom.cuts import cut_lines
from cutroom.render import export_plan
from cutroom.schemas import (
    CTA,
    ClipProposal,
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
level = Level.from_wav(Path(sys.argv[2]) if len(sys.argv) > 2 else D / "audio16k.wav")
# Optional third argument: the source video, so cover frames get a real face check.
faces = None
if len(sys.argv) > 3:
    from cutroom.faces import FaceProbe

    faces = FaceProbe(Path(sys.argv[3]))


def seg(first: float, last_end: float):
    """first/last_end: SRT times of the first and last line to keep. Cuts land in the gap
    between real words: never inside the first or last word, never on a neighbour."""
    ids = [s.id for s in srt.segments if s.start >= first - 0.05 and s.end <= last_end + 0.05]
    out, clean = cut_lines(t, ids, level, v)
    if not clean:
        print(f"  no clean pause at an edge of {first:.2f}-{last_end:.2f}")
    return out


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
                has_face=faces.has_face(round(segments[0].start + 1.5, 2)) if faces else None,
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
