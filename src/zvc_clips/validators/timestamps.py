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

    spec = rules.platforms.get(clip.platform)
    if spec is None:
        issues.append(
            Issue(
                code="UNKNOWN_PLATFORM",
                field="platform",
                message=f"no rules configured for {clip.platform}",
            )
        )
        return issues
    if not spec.min_seconds <= clip.duration <= spec.max_seconds:
        issues.append(
            Issue(
                code="BAD_LENGTH",
                field="end",
                message=f"{clip.duration:.1f}s is outside {clip.platform} range "
                f"{spec.min_seconds}-{spec.max_seconds}s",
            )
        )
    if clip.aspect_ratio != spec.aspect_ratio:
        issues.append(
            Issue(
                code="BAD_FORMAT",
                field="aspect_ratio",
                message=f"{clip.aspect_ratio} but {clip.platform} needs {spec.aspect_ratio}",
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
