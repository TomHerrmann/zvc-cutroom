from __future__ import annotations

from ..config import BrandRules
from ..schemas import ClipProposal, Issue, Severity, Transcript, VideoContext

# Seconds of a line that may sit outside a cut when the audio map proves the edge is silent.
SRT_SLOP = 0.35


def check_timestamps(
    clip: ClipProposal,
    transcript: Transcript,
    rules: BrandRules,
    video: VideoContext | None = None,
) -> list[Issue]:
    issues: list[Issue] = []
    if clip.video_id != transcript.video_id:
        issues.append(
            Issue(
                code="WRONG_VIDEO",
                field="video_id",
                message=f"clip is for {clip.video_id}, transcript is {transcript.video_id}",
            )
        )
    for i, g in enumerate(clip.segments):
        if g.end > transcript.duration:
            issues.append(
                Issue(
                    code="BAD_TIMESTAMP",
                    field=f"segments[{i}]",
                    message=f"segment ends at {g.end}s, past the end of the video "
                    f"({transcript.duration}s)",
                )
            )
    ordered = sorted(clip.segments, key=lambda g: g.start)
    for x, y in zip(ordered, ordered[1:], strict=False):
        if y.start < x.end:
            issues.append(
                Issue(
                    code="OVERLAPPING_SEGMENTS",
                    field="segments",
                    message=f"{x.start}-{x.end}s and {y.start}-{y.end}s overlap (repeated audio)",
                )
            )

    spec = rules.formats.get(clip.format)
    if spec is None:
        issues.append(
            Issue(
                code="UNKNOWN_FORMAT",
                field="format",
                message=f"no rules configured for {clip.format}",
            )
        )
        return issues
    # A clip proposal is always short form, and short form is always vertical.
    if spec.form != "short":
        issues.append(
            Issue(
                code="SHORT_MUST_BE_VERTICAL",
                field="format",
                message=f"clips are short form; {clip.format} is the {spec.form}-form format",
            )
        )
    total = clip.duration + (rules.end_card.seconds if clip.end_card and rules.end_card else 0)
    if not spec.min_seconds <= total <= spec.max_seconds:
        issues.append(
            Issue(
                code="BAD_LENGTH",
                field="end",
                message=f"{total:.1f}s is outside {clip.format} range "
                f"{spec.min_seconds}-{spec.max_seconds}s",
            )
        )
    if (clip.width, clip.height) != (spec.width, spec.height):
        issues.append(
            Issue(
                code="BAD_RESOLUTION",
                field="width",
                message=f"{clip.width}x{clip.height} but {clip.format} renders at "
                f"{spec.width}x{spec.height} for {spec.platform}",
            )
        )
    if clip.captions != spec.captions:
        issues.append(
            Issue(
                code="BAD_CAPTION_MODE",
                field="captions",
                message=f"{clip.format} uses {spec.captions.value} captions, "
                f"not {clip.captions.value}",
            )
        )

    # Cutting mid-word sounds broken. With a measured audio map this is an error;
    # with only transcript timing it is a warning.
    edges = [(g.start, f"segments[{i}].start") for i, g in enumerate(clip.segments)]
    edges += [(g.end, f"segments[{i}].end") for i, g in enumerate(clip.segments)]
    if video is not None and video.silences:
        for t, field in edges:
            if not video.in_silence(t):
                issues.append(
                    Issue(
                        code="CUT_MID_SPEECH",
                        field=field,
                        message=f"{field} at {t:.2f}s lands while someone is talking",
                    )
                )
    else:
        for w in transcript.words():
            for t, field in edges:
                if w.start < t < w.end and min(t - w.start, w.end - t) > 0.05:
                    issues.append(
                        Issue(
                            code="CUT_MID_WORD",
                            field=field,
                            severity=Severity.WARNING,
                            message=f"{field} {t}s cuts through {w.text!r}",
                        )
                    )
    # A segment must hold whole subtitle lines: a line that is only partly inside the cut
    # means words are clipped at the start or end. SRT line times are estimates, so when the
    # audio map shows the edge sits in a real pause, a sliver of up to SRT_SLOP seconds on the
    # wrong side of a line boundary is silence, not words, and is allowed.
    for i, g in enumerate(clip.segments):
        for cue in transcript.segments:
            overlap = min(cue.end, g.end) - max(cue.start, g.start)
            if 0.05 < overlap < (cue.end - cue.start) - 0.05:
                missing = (cue.end - cue.start) - overlap
                edge = g.start if cue.start < g.start else g.end
                if (
                    transcript.word_timing == "estimated"
                    and video is not None
                    and video.silences
                    and video.in_silence(edge)
                    and min(missing, overlap) <= SRT_SLOP
                ):
                    continue
                issues.append(
                    Issue(
                        code="PARTIAL_LINE",
                        field=f"segments[{i}]",
                        message=f"cuts through the line {cue.text!r} "
                        f"({cue.start:.2f}-{cue.end:.2f}s)",
                    )
                )
    if video is not None:
        for i, g in enumerate(clip.segments):
            for a, b in video.youtube_only:
                if g.start < b and a < g.end:
                    issues.append(
                        Issue(
                            code="YOUTUBE_ONLY_CONTENT",
                            field=f"segments[{i}]",
                            message=f"segment overlaps {a}-{b}s, which is long-form only "
                            "(e.g. the title card)",
                        )
                    )
    return issues
