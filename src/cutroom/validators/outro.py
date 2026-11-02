"""How a clip ends. If a longer review is on YouTube, the clip closes on the ZVC title card
pointing to it, and the spoken "let me know what you think" sign-off is cut."""

from __future__ import annotations

from ..config import BrandRules
from ..schemas import ClipProposal, Issue, Transcript, VideoContext
from .text import contains_sequence, normalize_tokens


def check_outro(
    clip: ClipProposal,
    transcript: Transcript,
    rules: BrandRules,
    video: VideoContext | None = None,
) -> list[Issue]:
    card = rules.end_card
    if card is None:
        return []
    issues: list[Issue] = []
    has_long = bool(video and video.long_review)

    if clip.end_card is not None and card.only_when_long_review and not has_long:
        issues.append(
            Issue(
                code="END_CARD_WITHOUT_LONG_REVIEW",
                field="end_card",
                message="end card points to a full review, but none is recorded for this video",
            )
        )
    if has_long and clip.end_card is None:
        issues.append(
            Issue(
                code="MISSING_END_CARD",
                field="end_card",
                message=f"a longer review exists; end on the {card.title}",
            )
        )
    if has_long:
        for seg in transcript.segments:
            inside = sum(
                max(0.0, min(seg.end, g.end) - max(seg.start, g.start)) for g in clip.segments
            )
            if inside < 0.5 * (seg.end - seg.start):
                continue
            tokens = normalize_tokens(seg.text)
            for phrase in card.replaces:
                if contains_sequence(tokens, normalize_tokens(phrase)):
                    issues.append(
                        Issue(
                            code="SPOKEN_OUTRO",
                            field="segments",
                            message=f'"{seg.text.strip()}" at {seg.start:.1f}s: the end card '
                            "replaces spoken sign-offs",
                        )
                    )
    return issues
