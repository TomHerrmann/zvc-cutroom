"""Brand checklist: CTA in the first seconds, face + 3-5 words on the cover,
no spoilers, hashtags and caption within format rules."""

from __future__ import annotations

import re

from ..config import BrandRules
from ..schemas import ClipProposal, Issue
from .text import contains_sequence, normalize_tokens


def check_brand(clip: ClipProposal, rules: BrandRules, extra_spoilers: list[str]) -> list[Issue]:
    issues: list[Issue] = []

    # CTA
    if clip.cta is None:
        if rules.cta.required:
            issues.append(Issue(code="MISSING_CTA", field="cta", message="clip has no CTA"))
    else:
        if clip.cta.kind not in rules.cta.allowed_kinds:
            issues.append(
                Issue(
                    code="BAD_CTA_KIND",
                    field="cta.kind",
                    message=f"CTA kind {clip.cta.kind} not allowed",
                )
            )
        if clip.cta.at > rules.cta.max_start_seconds:
            issues.append(
                Issue(
                    code="CTA_TOO_LATE",
                    field="cta.at",
                    message=f"CTA starts at {clip.cta.at:.1f}s, must be within "
                    f"{rules.cta.max_start_seconds:.0f}s",
                )
            )

    # Cover
    cover = clip.selected_cover()
    if cover is None:
        issues.append(
            Issue(
                code="NO_COVER_SELECTED",
                field="covers",
                message="no cover candidate is marked selected",
            )
        )
    else:
        if not clip.contains(cover.frame_time):
            issues.append(
                Issue(
                    code="COVER_OUTSIDE_CLIP",
                    field="covers",
                    message=f"cover frame {cover.frame_time}s is not inside the clip",
                )
            )
        if rules.cover.require_face and cover.has_face is not True:
            reason = "unverified" if cover.has_face is None else "no face"
            issues.append(
                Issue(
                    code="COVER_NO_FACE",
                    field="covers",
                    message=f"cover face check failed ({reason})",
                )
            )
        n = len(normalize_tokens(cover.overlay_text))
        if not rules.cover.min_words <= n <= rules.cover.max_words:
            issues.append(
                Issue(
                    code="COVER_WORD_COUNT",
                    field="covers",
                    message=f"cover text has {n} words, needs "
                    f"{rules.cover.min_words}-{rules.cover.max_words}",
                )
            )

    # Spoilers: checked on every public-facing text field.
    banned = rules.spoilers.banned_terms + extra_spoilers
    public = {
        "caption": clip.caption,
        "hook": clip.hook,
        "hashtags": " ".join(h.lstrip("#") for h in clip.hashtags),
        **{f"title_variants[{i}]": t for i, t in enumerate(clip.title_variants)},
        **{f"covers[{i}]": c.overlay_text for i, c in enumerate(clip.covers)},
    }
    for term in banned:
        needle = normalize_tokens(term)
        for field, text in public.items():
            if contains_sequence(normalize_tokens(text), needle):
                issues.append(
                    Issue(code="SPOILER", field=field, message=f"contains banned term {term!r}")
                )

    # Hashtags
    pat = re.compile(rules.hashtags.pattern)
    for h in clip.hashtags:
        if not pat.match(h):
            issues.append(
                Issue(
                    code="BAD_HASHTAG",
                    field="hashtags",
                    message=f"{h!r} does not match {rules.hashtags.pattern}",
                )
            )
    if len(clip.hashtags) > rules.hashtags.max_count:
        issues.append(
            Issue(
                code="TOO_MANY_HASHTAGS",
                field="hashtags",
                message=f"{len(clip.hashtags)} hashtags, max {rules.hashtags.max_count}",
            )
        )
    have = {h.lower() for h in clip.hashtags}
    for req in rules.hashtags.required:
        if req.lower() not in have:
            issues.append(
                Issue(code="MISSING_REQUIRED_HASHTAG", field="hashtags", message=f"missing {req}")
            )

    # Caption length per format
    spec = rules.formats.get(clip.format)
    if spec and len(clip.caption) > spec.caption_max_chars:
        issues.append(
            Issue(
                code="CAPTION_TOO_LONG",
                field="caption",
                message=f"{len(clip.caption)} chars, {clip.format} max {spec.caption_max_chars}",
            )
        )
    if not clip.caption.strip():
        issues.append(Issue(code="EMPTY_CAPTION", field="caption", message="caption is empty"))
    return issues
