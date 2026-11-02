"""No invented quotes, ever.

A quote passes only if its exact normalised word sequence appears in the
transcript inside its claimed time window (plus a small tolerance).
"""

from __future__ import annotations

from ..config import BrandRules
from ..schemas import ClipProposal, Issue, QuoteRef, Severity, Transcript
from .text import contains_sequence, normalize_tokens


def quote_in_transcript(quote: QuoteRef, transcript: Transcript, tolerance: float) -> bool:
    lo, hi = quote.start - tolerance, quote.end + tolerance
    window: list[str] = []
    if transcript.word_timing == "exact":
        for w in transcript.words():
            if w.end >= lo and w.start <= hi:
                window += normalize_tokens(w.text)
    else:
        # Resolve subtitle cues only have cue-level timing: search every cue that
        # overlaps the window. The quote still has to be verbatim and contiguous.
        for seg in transcript.segments:
            if seg.end >= lo and seg.start <= hi:
                window += normalize_tokens(seg.text)
    return contains_sequence(window, normalize_tokens(quote.text))


def check_quotes(clip: ClipProposal, transcript: Transcript, rules: BrandRules) -> list[Issue]:
    tol = rules.quotes.timestamp_tolerance_seconds
    issues: list[Issue] = []
    for i, q in enumerate(clip.quotes):
        field = f"quotes[{i}]"
        if q.end < q.start:
            issues.append(
                Issue(
                    code="QUOTE_BAD_TIMESTAMP",
                    field=field,
                    message=f"quote ends ({q.end}) before it starts ({q.start})",
                )
            )
            continue
        if not quote_in_transcript(q, transcript, tol):
            issues.append(
                Issue(
                    code="QUOTE_NOT_IN_TRANSCRIPT",
                    field=field,
                    message=f"{q.text!r} not found in transcript near {q.start:.1f}-{q.end:.1f}s",
                )
            )
            continue
        if q.start < clip.start - tol or q.end > clip.end + tol:
            issues.append(
                Issue(
                    code="QUOTE_OUTSIDE_CLIP",
                    field=field,
                    severity=Severity.WARNING,
                    message=f"{q.text!r} is real but falls outside the clip's cut",
                )
            )

    # A spoken CTA is a quote too: it has to actually be said, where the clip says it is.
    if clip.cta and clip.cta.kind == "spoken":
        at = clip.start + clip.cta.at
        n_words = max(len(normalize_tokens(clip.cta.text)), 1)
        probe = QuoteRef(text=clip.cta.text, start=at, end=at + 0.6 * n_words)
        if not quote_in_transcript(probe, transcript, tol):
            issues.append(
                Issue(
                    code="CTA_NOT_SPOKEN",
                    field="cta",
                    message=f"spoken CTA {clip.cta.text!r} not found at "
                    f"{clip.cta.at:.1f}s into the clip",
                )
            )
    return issues
