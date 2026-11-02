"""Validators. Each one takes a clip plus context and returns a list of Issues.

They are pure functions so the eval runner can call them on hand-labelled cases
exactly as the pipeline does.
"""

from __future__ import annotations

from ..config import BrandRules
from ..schemas import ClipProposal, Transcript, ValidationReport, VideoContext
from .brand import check_brand
from .quotes import check_quotes
from .timestamps import check_timestamps

__all__ = ["validate_clip", "check_brand", "check_quotes", "check_timestamps"]


def validate_clip(
    clip: ClipProposal,
    transcript: Transcript,
    rules: BrandRules,
    extra_spoilers: list[str] | None = None,
    video: VideoContext | None = None,
) -> ValidationReport:
    issues = []
    issues += check_timestamps(clip, transcript, rules, video)
    issues += check_quotes(clip, transcript, rules)
    issues += check_brand(clip, rules, extra_spoilers or [])
    return ValidationReport(clip_id=clip.id, issues=issues)
