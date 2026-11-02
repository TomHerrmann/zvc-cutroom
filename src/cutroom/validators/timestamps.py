from __future__ import annotations

from ..config import BrandRules
from ..schemas import ClipProposal, Issue, Severity, Transcript


def check_timestamps(clip: ClipProposal, transcript: Transcript, rules: BrandRules) -> list[Issue]:
    issues: list[Issue] = []
    if clip.video_id != transcript.video_id:
        issues.append(
            Issue(
                code="WRONG_VIDEO",
                field="video_id",
                message=f"clip is for {clip.video_id}, transcript is {transcript.video_id}",
            )
        )
    if clip.end <= clip.start:
        issues.append(
            Issue(
                code="BAD_TIMESTAMP",
                field="end",
                message=f"cut-out {clip.end} is not after cut-in {clip.start}",
            )
        )
        return issues
    if clip.end > transcript.duration:
        issues.append(
            Issue(
                code="BAD_TIMESTAMP",
                field="end",
                message=f"cut-out {clip.end}s is past the end of the video "
                f"({transcript.duration}s)",
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
    if not spec.min_seconds <= clip.duration <= spec.max_seconds:
        issues.append(
            Issue(
                code="BAD_LENGTH",
                field="end",
                message=f"{clip.duration:.1f}s is outside {clip.format} range "
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

    # Cutting mid-word sounds broken. Warn so a human can nudge the cut.
    for w in transcript.words():
        for edge, name in ((clip.start, "start"), (clip.end, "end")):
            if w.start < edge < w.end and min(edge - w.start, w.end - edge) > 0.05:
                issues.append(
                    Issue(
                        code="CUT_MID_WORD",
                        field=name,
                        severity=Severity.WARNING,
                        message=f"{name} {edge}s cuts through {w.text!r}",
                    )
                )
    return issues
