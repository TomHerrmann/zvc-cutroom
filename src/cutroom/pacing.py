"""Long-form pacing: keep the talking, speed up the work, cut the dead air.

Raw vlog footage has long stretches with no talking. Some are worth keeping (hands on the
projector, threading, spooling a reel) but not at real time; some are dead air (standing,
thinking, an empty frame). This module turns one raw take into an `EditPlan`: an ordered list
of source ranges, each with a playback speed.

Flow:
  1. The spoken lines to keep come from the draft as line-id ranges (one take of a repeated
     intro, no false starts), exactly like clip proposals. Consecutive kept lines with only
     a short pause between them form one spoken run, cut through `cuts.cut_lines`, so every
     edge sits in measured silence and spoken lines always play at real time.
  2. Each gap of at least `min_stretch_seconds` between two consecutive kept lines is an
     unspoken stretch. Claude sees a contact sheet of each stretch (frames every couple of
     seconds, with source times) and the lines either side, and labels it, in sub-ranges if
     it changes partway, with one kind: idle, empty, setup, handwork, repeat or show.
  3. Code picks the speed from the kind (`longform.pacing.speeds` in the brand rules), keeps
     short beats between lines gentle, steps long stretches up the speed levels until they
     fit `max_output_seconds`, and cuts any part of an active stretch where the frame is
     measured still (he walked off). Parts nobody labelled are cut and reported.
  4. `check_plan` gates the result: ordered, in bounds, allowed speeds, and no spoken word
     ever sped up or cut part-way.

Rendering is a plain ffmpeg script (`render_script`): one file per segment, then a concat.
Sped segments keep their sound pitch-corrected and lowered, so the room doesn't go dead.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from .config import PACE_KINDS, BrandRules, PacingRules
from .cuts import CutError, cut_lines
from .propose import LineRange
from .schemas import Issue, Segment, Severity, Transcript

if TYPE_CHECKING:  # pragma: no cover
    from .audio import Level
    from .runlog import Run

# Aligned word times are good to about a tenth of a second, and cut edges snap into the
# measured pause, which can sit a little inside the estimated word edge.
WORD_SLACK = 0.15

Kind = Literal["idle", "empty", "setup", "handwork", "repeat", "show"]
assert set(Kind.__args__) == set(PACE_KINDS)  # type: ignore[attr-defined]

KIND_HELP = {
    "idle": "he is in frame but not doing anything worth watching: standing, thinking, "
    "waiting, gearing up to talk, repeating himself",
    "empty": "nobody in frame, or the camera points at nothing",
    "setup": "big moves: unboxing, carrying, moving furniture, plugging in",
    "handwork": "hands on the projector or a reel: threading, fixing, adjusting",
    "repeat": "the same motion over and over: spooling, rewinding, the projector running",
    "show": "something worth seeing at real time with no words: the first light, the image "
    "on the screen, a reaction",
}


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- model output


class StretchLabel(_Out):
    start: float = Field(description="Source seconds where this kind of action starts.")
    end: float = Field(description="Source seconds where it ends.")
    kind: Kind
    note: str = Field(description="A few words on what is happening.")


class PacingDraft(_Out):
    keep: list[LineRange] = Field(description="Spoken line ranges to keep, in order.")
    labels: list[StretchLabel] = Field(
        description="One or more labels per unspoken stretch, covering it start to end."
    )


# --------------------------------------------------------------------------- plan


class EditSegment(_Out):
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    speed: float = Field(gt=0, description="1 is real time, 4 plays four times as fast.")
    kind: Literal["speech", "idle", "empty", "setup", "handwork", "repeat", "show"]
    lines: list[int] = Field(default_factory=list)
    note: str = ""

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def out_duration(self) -> float:
        return self.duration / self.speed


class Dropped(_Out):
    start: float
    end: float
    reason: str


class EditPlan(_Out):
    video_id: str
    source_duration: float
    segments: list[EditSegment]
    dropped: list[Dropped] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)
    rules_hash: str | None = None

    @property
    def out_duration(self) -> float:
        return sum(s.out_duration for s in self.segments)

    def out_time(self, t: float) -> float | None:
        """Where source time t lands in the cut, or None if it was cut."""
        acc = 0.0
        for s in self.segments:
            if s.start <= t <= s.end:
                return acc + (t - s.start) / s.speed
            acc += s.out_duration
        return None

    def summary(self) -> dict[str, Any]:
        by: dict[str, dict[str, float]] = {}
        for s in self.segments:
            key = "speech" if s.kind == "speech" else f"{s.kind} {s.speed:g}x"
            d = by.setdefault(key, {"source": 0.0, "out": 0.0, "count": 0})
            d["source"] += s.duration
            d["out"] += s.out_duration
            d["count"] += 1
        return {
            "source_seconds": round(self.source_duration, 1),
            "cut_seconds": round(self.out_duration, 1),
            "parts": {k: {kk: round(vv, 1) for kk, vv in v.items()} for k, v in by.items()},
            "dropped_seconds": round(sum(d.end - d.start for d in self.dropped), 1),
        }


# --------------------------------------------------------------------------- measurement


class Motion:
    """Share of pixels that changed between frames, sampled every `step` seconds."""

    def __init__(self, values: Sequence[float], step: float = 0.5):
        self.values = np.asarray(values, dtype=float)
        self.step = step

    @classmethod
    def from_video(cls, video: Path, step: float = 0.5, threshold: int = 12) -> Motion:
        import cv2  # `faces` extra

        cap = cv2.VideoCapture(str(video))
        if not cap.isOpened():
            raise ValueError(f"can't open {video}")
        out, prev, t = [], None, 0.0
        while True:
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
            ok, frame = cap.read()
            if not ok:
                break
            g = cv2.cvtColor(cv2.resize(frame, (160, 90)), cv2.COLOR_BGR2GRAY)
            g = cv2.GaussianBlur(g, (5, 5), 0).astype(np.float32)
            out.append(0.0 if prev is None else float((np.abs(g - prev) > threshold).mean()))
            prev, t = g, t + step
        cap.release()
        return cls(out, step)

    def still_runs(
        self, lo: float, hi: float, below: float, min_len: float
    ) -> list[tuple[float, float]]:
        """Runs inside [lo, hi] where motion stays under `below` for at least min_len."""
        runs, start = [], None
        i0, i1 = max(int(np.ceil(lo / self.step)), 0), min(int(hi / self.step), len(self.values))
        for i in range(i0, i1 + 1):
            still = i < i1 and self.values[i] < below
            if still and start is None:
                start = i
            elif not still and start is not None:
                a, b = start * self.step, min(i * self.step, hi)
                if b - a >= min_len:
                    runs.append((round(a, 3), round(b, 3)))
                start = None
        return runs


# --------------------------------------------------------------------------- stretches


class Stretch(_Out):
    start: float
    end: float
    before: int  # last line id before it
    after: int  # first line id after it

    @property
    def duration(self) -> float:
        return self.end - self.start


def kept_ids(transcript: Transcript, keep: Sequence[LineRange]) -> tuple[list[int], list[Issue]]:
    known = {s.id for s in transcript.segments}
    ids: list[int] = []
    issues = []
    for r in keep:
        if r.last < r.first:
            issues.append(Issue(code="BAD_LINE_RANGE", message=f"{r.first}-{r.last} is reversed"))
            continue
        span = list(range(r.first, r.last + 1))
        missing = [i for i in span if i not in known]
        if missing:
            issues.append(Issue(code="UNKNOWN_LINE", message=f"unknown line ids {missing}"))
        ids += [i for i in span if i in known and i not in ids]
    return sorted(ids), issues


def spoken_runs(transcript: Transcript, ids: Sequence[int], rules: PacingRules) -> list[list[int]]:
    """Kept lines grouped into runs: consecutive ids with a short pause between them."""
    by = {s.id: s for s in transcript.segments}
    voiced = [i for i in ids if by[i].words]
    runs: list[list[int]] = []
    for i in voiced:
        if runs:
            prev = by[runs[-1][-1]]
            gap = by[i].words[0].start - prev.words[-1].end
            if i == runs[-1][-1] + 1 and gap < rules.min_stretch_seconds:
                runs[-1].append(i)
                continue
        runs.append([i])
    return runs


def _run_cuts(
    transcript: Transcript, runs: list[list[int]], level: Level | None
) -> list[tuple[float, float, bool]]:
    out = []
    for r in runs:
        seg, clean = cut_lines(transcript, r, level)
        out.append((seg.start, seg.end, clean))
    return out


def find_stretches(
    transcript: Transcript,
    keep: Sequence[LineRange],
    rules: PacingRules,
    level: Level | None = None,
) -> list[Stretch]:
    """The unspoken stretches between consecutive kept lines that the draft must label."""
    ids, _ = kept_ids(transcript, keep)
    runs = spoken_runs(transcript, ids, rules)
    cuts = _run_cuts(transcript, runs, level)
    out = []
    pairs = list(zip(runs, cuts, strict=True))
    for (ra, ca), (rb, cb) in zip(pairs, pairs[1:], strict=False):
        if rb[0] == ra[-1] + 1 and cb[0] > ca[1]:
            out.append(Stretch(start=ca[1], end=cb[0], before=ra[-1], after=rb[0]))
    return out


# --------------------------------------------------------------------------- planning


def _speed_for(kind: str, piece: float, stretch: float, rules: PacingRules) -> float:
    speed = rules.speeds[kind]
    if speed <= 1:
        return speed
    if stretch < rules.short_stretch_seconds:
        speed = min(speed, rules.short_max_speed)
    for lvl in rules.levels:
        if lvl > speed and piece / speed > rules.max_output_seconds:
            speed = lvl
    return speed


def build_plan(
    transcript: Transcript,
    draft: PacingDraft,
    rules: BrandRules,
    level: Level | None = None,
    motion: Motion | None = None,
) -> EditPlan:
    pr = rules.longform.pacing
    ids, issues = kept_ids(transcript, draft.keep)
    runs = spoken_runs(transcript, ids, pr)
    cuts = _run_cuts(transcript, runs, level)
    stretches = find_stretches(transcript, draft.keep, pr, level)
    by_after = {s.after: s for s in stretches}
    segments: list[EditSegment] = []
    dropped: list[Dropped] = []

    used: set[int] = set()
    labels = sorted(enumerate(draft.labels), key=lambda x: x[1].start)
    for n, lab in labels:
        if lab.end <= lab.start:
            issues.append(Issue(code="BAD_LABEL", message=f"label {n} ends before it starts"))
            used.add(n)

    def pieces(st: Stretch) -> list[EditSegment]:
        mine = []
        for n, lab in labels:
            if n in used or lab.end <= st.start or lab.start >= st.end:
                continue
            used.add(n)
            a, b = max(lab.start, st.start), min(lab.end, st.end)
            if mine and a < mine[-1].end:
                issues.append(
                    Issue(
                        code="LABEL_OVERLAP",
                        message=f"labels overlap at {a:.2f}s; the later one starts after",
                        severity=Severity.WARNING,
                    )
                )
                a = mine[-1].end
            if b - a > 0.01:
                mine.append(
                    EditSegment(
                        start=round(a, 3), end=round(b, 3), speed=1, kind=lab.kind, note=lab.note
                    )
                )
        # gaps nobody labelled are cut, and the reviewer is told
        t = st.start
        for p in [*mine, None]:
            nxt = p.start if p else st.end
            if nxt - t > 0.25:
                dropped.append(Dropped(start=round(t, 3), end=round(nxt, 3), reason="unlabelled"))
                issues.append(
                    Issue(
                        code="UNLABELLED_STRETCH",
                        message=f"{t:.1f}-{nxt:.1f}s between lines {st.before} and {st.after} "
                        "had no label and was cut",
                        severity=Severity.WARNING,
                    )
                )
            if p:
                t = p.end
        out = []
        for p in mine:
            if pr.speeds[p.kind] == 0:
                dropped.append(Dropped(start=p.start, end=p.end, reason=p.kind))
                continue
            parts = [(p.start, p.end)]
            if motion is not None and p.kind != "show":
                parts = []
                t = p.start
                for a, b in motion.still_runs(
                    p.start, p.end, pr.still_motion, pr.still_min_seconds
                ):
                    dropped.append(Dropped(start=a, end=b, reason="still frame"))
                    if a > t:
                        parts.append((t, a))
                    t = b
                if p.end > t:
                    parts.append((t, p.end))
            for a, b in parts:
                speed = _speed_for(p.kind, b - a, st.duration, pr)
                if speed > 1 and (b - a) / speed < pr.min_output_seconds:
                    dropped.append(Dropped(start=a, end=b, reason="too short once sped up"))
                    continue
                out.append(p.model_copy(update={"start": a, "end": b, "speed": speed}))
        return out

    for run, (a, b, clean) in zip(runs, cuts, strict=True):
        st = by_after.get(run[0])
        if st is not None:
            segments += pieces(st)
        if not clean:
            issues.append(
                Issue(
                    code="CUT_MID_SPEECH",
                    message=f"lines {run[0]}-{run[-1]}: no clean pause found at an edge",
                    severity=Severity.WARNING,
                )
            )
        segments.append(EditSegment(start=a, end=b, speed=1, kind="speech", lines=run))

    for n, lab in labels:
        if n not in used:
            issues.append(
                Issue(
                    code="LABEL_OUTSIDE_STRETCH",
                    message=f"label {lab.start:.1f}-{lab.end:.1f}s ({lab.kind}) is not inside "
                    "an unspoken stretch between kept lines",
                    severity=Severity.WARNING,
                )
            )
    plan = EditPlan(
        video_id=transcript.video_id,
        source_duration=transcript.duration,
        segments=segments,
        dropped=dropped,
        issues=issues,
        rules_hash=rules.config_hash,
    )
    plan.issues += check_plan(plan, transcript, rules)
    return plan


def check_plan(plan: EditPlan, transcript: Transcript, rules: BrandRules) -> list[Issue]:
    """Hard checks on a plan, however it was made."""
    pr = rules.longform.pacing
    out: list[Issue] = []
    prev = None
    for s in plan.segments:
        if s.end <= s.start or s.start < 0 or s.end > plan.source_duration + 0.05:
            out.append(Issue(code="SEGMENT_OUT_OF_BOUNDS", message=f"{s.start}-{s.end}"))
        if prev is not None and s.start < prev.end - 1e-3:
            out.append(Issue(code="SEGMENTS_OUT_OF_ORDER", message=f"{s.start} < {prev.end}"))
        if s.speed not in pr.levels:
            out.append(Issue(code="BAD_SPEED", message=f"{s.speed}x at {s.start}"))
        if s.kind == "speech" and s.speed != 1:
            out.append(Issue(code="SPEECH_NOT_REALTIME", message=f"lines {s.lines} at {s.speed}x"))
        prev = s
    tol = WORD_SLACK
    realtime = [s for s in plan.segments if s.speed == 1]
    sped = [s for s in plan.segments if s.speed != 1]
    kept = {i for s in plan.segments for i in s.lines}
    for line in transcript.segments:
        for w in line.words:
            if any(s.start < w.end - tol and w.start + tol < s.end for s in sped):
                out.append(
                    Issue(
                        code="SPEECH_SPED_UP", message=f"line {line.id} word {w.text!r} is sped up"
                    )
                )
                break
        if line.id in kept and line.words:
            a, b = line.words[0].start, line.words[-1].end
            if not any(s.start <= a + tol and b - tol <= s.end for s in realtime):
                out.append(Issue(code="LINE_CUT", message=f"line {line.id} is not played whole"))
    return out


# --------------------------------------------------------------------------- outputs


def retimed(plan: EditPlan, transcript: Transcript) -> Transcript:
    """The kept lines on the cut's timeline: the YouTube caption file for this edit."""
    kept = {i for s in plan.segments for i in s.lines}
    segs = []
    for line in transcript.segments:
        if line.id not in kept:
            continue
        a = plan.out_time(line.words[0].start if line.words else line.start)
        b = plan.out_time(line.words[-1].end if line.words else line.end)
        if a is None or b is None:
            continue
        segs.append(
            Segment(id=line.id, start=round(a, 3), end=round(max(b, a + 0.3), 3), text=line.text)
        )
    return Transcript(
        video_id=transcript.video_id,
        source="resolve_reviewed",
        duration=max(plan.out_duration, 0.1),
        word_timing="estimated",
        segments=segs,
    )


def _atempo(speed: float) -> str:
    parts, s = [], speed
    while s > 2.0 + 1e-9:
        parts.append("atempo=2.0")
        s /= 2.0
    parts.append(f"atempo={s:g}")
    return ",".join(parts)


def render_script(
    plan: EditPlan,
    source: str,
    rules: BrandRules,
    width: int = 1920,
    height: int = 1080,
    fps: int = 30,
) -> str:
    """Bash that renders a review copy: one file per segment, then a concat.

    `render.sh` renders every missing segment; `render.sh A B` only segments A..B (so a long
    job can run in pieces); `render.sh concat` joins them into cut.mp4.
    """
    vol = rules.longform.pacing.sped_audio_volume
    lines = [
        "#!/bin/bash",
        f"# Review cut of {plan.video_id}: {len(plan.segments)} segments, "
        f"{plan.out_duration / 60:.1f} min from {plan.source_duration / 60:.1f} min of source.",
        "set -e",
        'cd "$(dirname "$0")"',
        f'SRC="{source}"',
        "mkdir -p segs",
        'if [ "$1" = concat ]; then',
        "  ls segs/*.mp4 | sort | sed \"s/^/file '/;s/$/'/\" > segs/list.txt",
        "  ffmpeg -nostdin -v error -y -f concat -safe 0 -i segs/list.txt -c copy "
        "-movflags +faststart cut.mp4",
        "  exit 0",
        "fi",
        "FROM=${1:-0}; TO=${2:-99999}",
        "seg() {  # index start duration video-filter audio-filter",
        '  [ "$1" -lt "$FROM" ] || [ "$1" -gt "$TO" ] && return 0',
        '  OUT=$(printf "segs/%04d.mp4" "$1")',
        '  [ -s "$OUT" ] && return 0',
        '  ffmpeg -nostdin -v error -y -ss "$2" -t "$3" -i "$SRC" -map 0:v:0 -map 0:a:0 '
        '-vf "$4" -af "$5" -c:v libx264 -preset veryfast -crf 22 -pix_fmt yuv420p '
        '-c:a aac -b:a 160k -ar 48000 -ac 2 -f mp4 "$OUT.part" && mv "$OUT.part" "$OUT"',
        "}",
    ]
    for i, s in enumerate(plan.segments):
        scale = f"scale={width}:{height}"
        if s.speed == 1:
            vf, af = f"{scale},fps={fps}", "anull"
        else:
            vf = f"setpts=(PTS-STARTPTS)/{s.speed:g},{scale},fps={fps}"
            af = f"{_atempo(s.speed)},volume={vol:g}"
        tag = f"speech {s.lines[0]}-{s.lines[-1]}" if s.lines else f"{s.kind} {s.speed:g}x"
        lines.append(f'seg {i} {s.start:.3f} {s.duration:.3f} "{vf}" "{af}"  # {tag}')
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- Claude


def stretch_sheet(video: Path, st: Stretch, every: float = 2.0, max_frames: int = 16) -> bytes:
    """A JPEG grid of frames across the stretch, each stamped with its source time."""
    import cv2

    cap = cv2.VideoCapture(str(video))
    n = int(min(max_frames, max(2, st.duration // every + 1)))
    thumbs = []
    for t in np.linspace(st.start + 0.2, st.end - 0.2, n):
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, frame = cap.read()
        if not ok:
            continue
        f = cv2.resize(frame, (320, 180))
        cv2.putText(f, f"{t:.1f}", (6, 22), 0, 0.7, (0, 255, 255), 2)
        thumbs.append(f)
    cap.release()
    if not thumbs:
        raise ValueError(f"no frames readable in {st.start}-{st.end}")
    cols = 4
    while len(thumbs) % cols:
        thumbs.append(np.zeros_like(thumbs[0]))
    grid = np.vstack([np.hstack(thumbs[i : i + cols]) for i in range(0, len(thumbs), cols)])
    ok, buf = cv2.imencode(".jpg", grid, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return buf.tobytes()


SYSTEM = """\
You edit raw footage into a long-form YouTube video for {brand}. {about}

Talking: keep the spoken lines that belong in the video, as line-id ranges. When a line or \
an intro was recorded several times, keep one take (usually the last full one) and drop the \
others, along with false starts and lines that only repeat what was just said. Keep the \
sign-off. Lines are never sped up or cut part-way; that is handled for you.

Unspoken stretches: each stretch below is a gap with no talking between two kept lines, \
shown as frames stamped with source seconds. Label every stretch from its start to its end \
with one or more labels, splitting it where the action changes. Kinds:
{kinds}
Idle and empty stretches are cut; the others are kept, sped up by kind. Label what you see, \
not what you'd like to keep: the speed is decided by code.
"""


def _system(rules: BrandRules, about: str | None) -> str:
    kinds = "\n".join(f"- {k}: {v}" for k, v in KIND_HELP.items())
    return SYSTEM.format(brand=rules.brand, about=about or "", kinds=kinds)


def _lines_text(transcript: Transcript) -> str:
    return "\n".join(f"[{s.id}] {s.start:.1f}-{s.end:.1f} {s.text}" for s in transcript.segments)


def draft_with_claude(
    rules: BrandRules,
    transcript: Transcript,
    keep: Sequence[LineRange],
    video: Path,
    level: Level | None = None,
    about: str | None = None,
    run: Run | None = None,
    client: Any = None,
) -> PacingDraft:
    """One call: the transcript plus a contact sheet per stretch.

    The stretches depend on which lines are kept, so `keep` is a first guess (all lines, or a
    previous draft's); Claude may change it, and any stretch the new keep list opens that has
    no label is cut and reported by `build_plan`.
    """
    import anthropic

    pr = rules.longform.pacing
    client = client or anthropic.Anthropic()
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": "Subtitle lines, `[id] start-end text`:\n" + _lines_text(transcript),
        }
    ]
    for st in find_stretches(transcript, keep, pr, level):
        content.append(
            {
                "type": "text",
                "text": f"Stretch {st.start:.1f}-{st.end:.1f}s, after line {st.before}, "
                f"before line {st.after}:",
            }
        )
        data = base64.standard_b64encode(stretch_sheet(video, st)).decode()
        content.append(
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": data},
            }
        )
    response = client.beta.messages.parse(
        model=pr.model,
        max_tokens=16000,
        system=_system(rules, about),
        messages=[{"role": "user", "content": content}],
        output_format=PacingDraft,
        output_config={"effort": pr.effort},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if run is not None:
        run.add_usage(response.model, response.usage.input_tokens, response.usage.output_tokens)
    if response.stop_reason == "refusal":
        raise RuntimeError("pacing draft was declined by the model")
    if response.stop_reason == "max_tokens" or response.parsed_output is None:
        raise RuntimeError(f"pacing draft incomplete (stop_reason={response.stop_reason})")
    return response.parsed_output


def save(obj: BaseModel, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj.model_dump(mode="json"), indent=1) + "\n")
    return path


__all__ = [
    "CutError",
    "Dropped",
    "EditPlan",
    "EditSegment",
    "Motion",
    "PacingDraft",
    "Stretch",
    "StretchLabel",
    "build_plan",
    "check_plan",
    "draft_with_claude",
    "find_stretches",
    "render_script",
    "retimed",
]
