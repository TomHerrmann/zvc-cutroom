"""Milestone 2: Claude scores moments and drafts clip proposals; code and validators check them.

Flow:
  1. `draft_with_claude` sends the reviewed transcript, one numbered line per subtitle cue,
     plus the brand rules rendered as plain instructions. Claude returns a `ProposalDraft`:
     per moment, which lines to play (as line-id ranges, never timestamps), the hook, caption,
     hashtags, titles, cover text, verbatim quotes, and 0-1 scores for hook, quote punch and
     theme.
  2. `compile_draft` turns each moment into a `ClipProposal`. Line ranges become cuts snapped
     into measured silence (`cuts.cut_lines`). Quote timestamps come from the cue ids Claude
     names, so a quote is only as good as its text: the quote validator then checks the text is
     really said there. Code measures what the model can't see: face_on_screen and the cover's
     face (`faces.py`, from the video file) and length_fit.
  3. Every clip runs through `validate_clip`. Failures go back to Claude with the exact issues
     (`repair_with_claude`), up to `max_repairs` rounds. What still fails is kept as
     rejected_by_validator so the reviewer can see what the model tried.
  4. Passing clips are ranked by a weighted mean of their scores (weights in the brand rules)
     and queued as pending_review. Nothing is approved or posted automatically.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from .config import BrandRules
from .cuts import CutError, cut_lines
from .schemas import (
    CTA,
    ClipProposal,
    ClipSegment,
    ClipStatus,
    CoverCandidate,
    Issue,
    MomentScores,
    QuoteRef,
    Transcript,
    ValidationReport,
    VideoContext,
)
from .validators import validate_clip
from .validators.text import contains_sequence, normalize_tokens

if TYPE_CHECKING:  # pragma: no cover
    from .audio import Level
    from .faces import FaceProbe
    from .runlog import Run


# --------------------------------------------------------------------------- model output


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LineRange(_Out):
    first: int = Field(description="Line id of the first line to play.")
    last: int = Field(description="Line id of the last line to play (same as first for one).")


class DraftQuote(_Out):
    first_line: int = Field(description="Line id where the quote starts.")
    last_line: int = Field(description="Line id where the quote ends (same line is fine).")
    text: str = Field(description="The words exactly as they appear in those lines.")


class DraftScores(_Out):
    hook: float = Field(description="0-1. Would the first 3 seconds stop a scroll?")
    quote_punch: float = Field(description="0-1. Is there a line people would repeat?")
    theme: float = Field(description="0-1. Does it carry one clear idea from the review?")


class MomentDraft(_Out):
    slug: str = Field(description="Short kebab-case name, e.g. 'who-decides'.")
    lines: list[LineRange] = Field(description="Line ranges to play, in playback order.")
    theme: str = Field(description="The one idea this clip carries, in a few words.")
    hook: str = Field(description="What the first seconds are selling, in one line.")
    caption: str = Field(description="Post caption.")
    hashtags: list[str]
    title_variants: list[str] = Field(description="One to three title options.")
    quotes: list[DraftQuote] = Field(description="The strongest verbatim lines in the clip.")
    cover_line: int = Field(description="Line id whose frame should be the cover.")
    cover_text: str = Field(description="Cover overlay text.")
    scores: DraftScores
    why: str = Field(description="One sentence: why this moment works as a short.")


class ProposalDraft(_Out):
    moments: list[MomentDraft]


# --------------------------------------------------------------------------- prompt


SYSTEM = """\
You pick and package short vertical clips from long-form videos for {brand}, a film \
channel. Videos come in different formats (reviews, essays, vlogs, behind-the-scenes, \
interviews); judge each moment by what works for this video's format, not by what a \
review would need. You are given the video's reviewed subtitle lines, one per row as \
`[id] start-end text`. Pick the {count} strongest standalone moments, best first, \
and draft each one.
{about}
How a clip is built:
- A clip plays one or more line ranges in the order you list them ({max_segments} at most). \
Each range is whole lines, first to last id inclusive. You may stitch ranges from different \
parts of the video, but the result must follow one train of thought and make sense to \
someone who never saw the video. Ranges must not overlap.
- Spoken length must be {lo:.0f}-{hi:.0f} seconds (hard limits {fmin:.0f}-{fmax:.0f} \
seconds{card_note}). Use the line times to add it up.
- Cuts need a pause. A range can't end on a line marked NO PAUSE AFTER, and can't start on \
the line right after one.
- Open on the line that hooks hardest. Never open mid-thought ("And so...", "Which is why...").
{card_rule}\
- Never use lines marked LONG-FORM ONLY.
- Leave out the creator's channel plugs (follow, subscribe, comment) and setup that only \
makes sense with the whole video.

Copy:
- quotes: the clip's strongest lines, copied exactly as written in the lines you cite, \
inside the clip. Never paraphrase, fix grammar, or join words that aren't adjacent.
- cover_text: {cmin}-{cmax} words. cover_line: a line inside the clip where the speaker is \
on camera mid-thought.
- hashtags: at most {max_tags}, each matching {tag_pattern}, always including {required}.
- caption: under {cap_chars} characters, in the creator's voice, no spoilers{spoilers}.
- Scores are 0-1 and honest: most moments are not a 1. They rank the clips against each \
other.
"""


def _system_prompt(
    rules: BrandRules, video: VideoContext | None, spoilers: list[str], about: str | None = None
) -> str:
    p = rules.proposals
    spec = rules.formats["vertical"]
    card = rules.end_card
    long_review = bool(video and video.long_review)
    if card and long_review:
        card_note = f", including the {card.seconds:.0f}-second end card"
        card_rule = (
            f'- Every clip ends on the "{card.topic_title}" end card, which replaces spoken '
            "sign-offs. Never include lines marked SIGN-OFF.\n"
        )
    else:
        card_note, card_rule = "", ""
    banned = rules.spoilers.banned_terms + spoilers
    return SYSTEM.format(
        brand=rules.brand,
        about=f"\nAbout this video: {about.strip()}\n" if about else "",
        count=p.count,
        max_segments=p.max_segments,
        lo=p.target_seconds[0],
        hi=p.target_seconds[1],
        fmin=spec.min_seconds,
        fmax=spec.max_seconds,
        card_note=card_note,
        card_rule=card_rule,
        cmin=rules.cover.min_words,
        cmax=rules.cover.max_words,
        max_tags=rules.hashtags.max_count,
        tag_pattern=rules.hashtags.pattern,
        required=", ".join(rules.hashtags.required) or "nothing in particular",
        cap_chars=min(spec.caption_max_chars, 300),
        spoilers=f" (never mention: {', '.join(banned)})" if banned else "",
    )


def _marks(rules: BrandRules, transcript: Transcript, video: VideoContext | None) -> dict[int, str]:
    out: dict[int, str] = {}
    signoffs = [normalize_tokens(p) for p in (rules.end_card.replaces if rules.end_card else [])]
    for s in transcript.segments:
        if video and any(s.start < b and a < s.end for a, b in video.youtube_only):
            out[s.id] = "LONG-FORM ONLY"
        elif video and video.long_review:
            toks = normalize_tokens(s.text)
            if any(contains_sequence(toks, p) for p in signoffs):
                out[s.id] = "SIGN-OFF"
    return out


def run_ons(
    transcript: Transcript, video: VideoContext | None, level: Level | None = None
) -> set[int]:
    """Lines that can't end a cut: the cut after them, or before the next line, would land
    mid-speech. Uses the same cut placement as `compile_draft`, so the hint and the check agree."""
    if video is None or not video.silences:
        return set()
    lines = [s for s in transcript.segments if s.words]
    cuts: dict[int, ClipSegment] = {}
    for s in lines:
        try:
            cuts[s.id] = cut_lines(transcript, [s.id], level, video)[0]
        except CutError:  # long-form-only lines are marked as such
            continue
    out = set()
    for a, b in zip(lines, lines[1:], strict=False):
        if a.id not in cuts or b.id not in cuts:
            continue
        if not video.in_silence(cuts[a.id].end) or not video.in_silence(cuts[b.id].start):
            out.add(a.id)
    return out


def transcript_prompt(
    rules: BrandRules,
    transcript: Transcript,
    video: VideoContext | None,
    level: Level | None = None,
) -> str:
    marks = _marks(rules, transcript, video)
    no_pause = run_ons(transcript, video, level)
    rows = []
    for s in transcript.segments:
        tags = [marks[s.id]] if s.id in marks else []
        if s.id in no_pause:
            tags.append("NO PAUSE AFTER")
        tag = f"  ({', '.join(tags)})" if tags else ""
        rows.append(f"[{s.id}] {s.start:.2f}-{s.end:.2f} {s.text.strip()}{tag}")
    return f"Video: {transcript.video_id}\n\nLines:\n" + "\n".join(rows)


# --------------------------------------------------------------------------- calls


def _call(
    client: Any,
    rules: BrandRules,
    system: str,
    user: str,
    run: Run | None,
) -> ProposalDraft:
    response = client.beta.messages.parse(
        model=rules.proposals.model,
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_format=ProposalDraft,
        output_config={"effort": rules.proposals.effort},
        # On a safety decline, the API re-runs the request on a fallback model.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if run is not None:
        run.add_usage(response.model, response.usage.input_tokens, response.usage.output_tokens)
    if response.stop_reason == "refusal":
        raise RuntimeError("clip proposal was declined by the model")
    if response.stop_reason == "max_tokens" or response.parsed_output is None:
        raise RuntimeError(f"clip proposal incomplete (stop_reason={response.stop_reason})")
    return response.parsed_output


def draft_with_claude(
    rules: BrandRules,
    transcript: Transcript,
    video: VideoContext | None = None,
    spoilers: list[str] | None = None,
    level: Level | None = None,
    about: str | None = None,
    run: Run | None = None,
    client: Any = None,
) -> ProposalDraft:
    import anthropic

    client = client or anthropic.Anthropic()
    system = _system_prompt(rules, video, spoilers or [], about)
    return _call(client, rules, system, transcript_prompt(rules, transcript, video, level), run)


def repair_prompt(failed: list[tuple[MomentDraft, list[Issue]]]) -> str:
    parts = [
        "The automatic checks rejected these drafts. Fix each one so it passes, keeping its "
        "slug and its idea. If a moment can't be fixed, leave it out. Return only these "
        "moments.\n"
    ]
    for m, issues in failed:
        parts.append(f"Draft:\n{m.model_dump_json()}\nRejected for:")
        parts += [f"- {i.code}: {i.message}" for i in issues]
        parts.append("")
    return "\n".join(parts)


def repair_with_claude(
    rules: BrandRules,
    transcript: Transcript,
    failed: list[tuple[MomentDraft, list[Issue]]],
    video: VideoContext | None = None,
    spoilers: list[str] | None = None,
    level: Level | None = None,
    about: str | None = None,
    run: Run | None = None,
    client: Any = None,
) -> ProposalDraft:
    import anthropic

    client = client or anthropic.Anthropic()
    system = _system_prompt(rules, video, spoilers or [], about)
    user = transcript_prompt(rules, transcript, video, level) + "\n\n" + repair_prompt(failed)
    return _call(client, rules, system, user, run)


# --------------------------------------------------------------------------- compile


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:40].strip("-") or "moment"


def length_fit(seconds: float, rules: BrandRules) -> float:
    """1 inside the target band, falling linearly to 0 at the format's hard limits."""
    lo, hi = rules.proposals.target_seconds
    spec = rules.formats["vertical"]
    if lo <= seconds <= hi:
        return 1.0
    if seconds < lo:
        span = lo - spec.min_seconds
        return round(max(0.0, (seconds - spec.min_seconds) / span), 3) if span > 0 else 0.0
    span = spec.max_seconds - hi
    return round(max(0.0, (spec.max_seconds - seconds) / span), 3) if span > 0 else 0.0


def rank_score(scores: MomentScores, rules: BrandRules) -> float:
    w = rules.proposals.weights
    got = {k: getattr(scores, k) for k in w if getattr(scores, k) is not None}
    total = sum(w[k] for k in got)
    return round(sum(w[k] * v for k, v in got.items()) / total, 4) if total else 0.0


def _clamp01(x: float) -> float:
    return min(max(x, 0.0), 1.0)


def nearest_face(
    faces: FaceProbe, segments: list[ClipSegment], t: float, step: float = 0.5
) -> float | None:
    """The frame closest to t, inside the clip, that shows a face (sampled every `step`)."""
    times = []
    for g in segments:
        x = g.start + 0.25
        while x < g.end - 0.25:
            times.append(round(x, 2))
            x += step
    for x in sorted(times, key=lambda x: abs(x - t)):
        if faces.has_face(x):
            return x
    return None


def compile_draft(
    m: MomentDraft,
    transcript: Transcript,
    rules: BrandRules,
    video: VideoContext | None = None,
    level: Level | None = None,
    faces: FaceProbe | None = None,
    clip_id: str | None = None,
) -> tuple[ClipProposal | None, list[Issue]]:
    """A model draft -> a ClipProposal, or the reasons it can't become one."""
    cues = {s.id: s for s in transcript.segments}
    issues: list[Issue] = []
    if not m.lines:
        return None, [Issue(code="NO_LINES", field="lines", message="the draft plays no lines")]
    if len(m.lines) > rules.proposals.max_segments:
        issues.append(
            Issue(
                code="TOO_MANY_SEGMENTS",
                field="lines",
                message=f"{len(m.lines)} line ranges, at most {rules.proposals.max_segments}",
            )
        )
    segments: list[ClipSegment] = []
    for i, r in enumerate(m.lines):
        if r.last < r.first:
            issues.append(
                Issue(
                    code="BAD_LINE_RANGE",
                    field=f"lines[{i}]",
                    message=f"range {r.first}-{r.last} ends before it starts",
                )
            )
            continue
        try:
            seg, _ = cut_lines(transcript, range(r.first, r.last + 1), level, video)
        except CutError as e:
            issues.append(Issue(code=e.code, field=f"lines[{i}]", message=str(e)))
            continue
        segments.append(seg)
    if issues:
        return None, issues

    quotes = []
    for q in m.quotes:
        a, b = cues.get(q.first_line), cues.get(q.last_line)
        if a is None or b is None or b.end < a.start:
            issues.append(
                Issue(
                    code="QUOTE_BAD_LINES",
                    field="quotes",
                    message=f"quote {q.text!r} cites lines {q.first_line}-{q.last_line}, "
                    "which don't exist",
                )
            )
            continue
        quotes.append(QuoteRef(text=q.text, start=a.start, end=b.end))

    long_review = bool(video and video.long_review)
    card = rules.end_card.topic_title if rules.end_card and long_review else None
    clip = ClipProposal(
        id=clip_id or f"{rules.proposals.clip_prefix}{_slug(m.slug)}",
        video_id=transcript.video_id,
        segments=segments,
        hook=m.hook,
        caption=m.caption,
        hashtags=m.hashtags,
        title_variants=m.title_variants,
        quotes=quotes,
        cta=CTA(**rules.proposals.cta.model_dump()),
        end_card=card,
        created_by="llm",
    )
    # Cover frame: the middle of the line Claude picked, if it's in the cut; else early on.
    cue = cues.get(m.cover_line)
    t = round((cue.start + cue.end) / 2, 2) if cue else None
    if t is None or not clip.contains(t):
        t = round(segments[0].start + min(1.5, segments[0].duration / 2), 2)
    has_face = faces.has_face(t) if faces else None
    if faces and has_face is False:  # the model can't see the frame; find the nearest face
        t2 = nearest_face(faces, segments, t)
        if t2 is not None:
            t, has_face = t2, True
    clip.covers = [
        CoverCandidate(frame_time=t, overlay_text=m.cover_text, has_face=has_face, selected=True)
    ]
    total = clip.duration + (rules.end_card.seconds if clip.end_card and rules.end_card else 0)
    clip.scores = MomentScores(
        hook=_clamp01(m.scores.hook),
        quote_punch=_clamp01(m.scores.quote_punch),
        theme=_clamp01(m.scores.theme),
        length_fit=length_fit(total, rules),
        face_on_screen=faces.share(segments) if faces else None,
    )
    return clip, issues


# --------------------------------------------------------------------------- pipeline


class Proposed(BaseModel):
    """One draft's outcome: the clip (if it compiled), its report, and the rank score."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    slug: str
    draft: MomentDraft
    clip: ClipProposal | None
    issues: list[Issue]
    report: ValidationReport | None
    rank: float = 0.0
    attempts: int = 1

    @property
    def passed(self) -> bool:
        return self.clip is not None and self.report is not None and self.report.passed

    def problems(self) -> list[Issue]:
        errors = [i for i in self.issues if i.severity.value == "error"]
        if self.report is not None:
            errors += [i for i in self.report.issues if i.severity.value == "error"]
        return errors


def check_draft(
    m: MomentDraft,
    transcript: Transcript,
    rules: BrandRules,
    video: VideoContext | None = None,
    level: Level | None = None,
    faces: FaceProbe | None = None,
    spoilers: list[str] | None = None,
    clip_id: str | None = None,
) -> Proposed:
    clip, issues = compile_draft(m, transcript, rules, video, level, faces, clip_id)
    report = validate_clip(clip, transcript, rules, spoilers, video=video) if clip else None
    if clip is not None and report is not None:
        clip.status = (
            ClipStatus.PENDING_REVIEW if report.passed else ClipStatus.REJECTED_BY_VALIDATOR
        )
    return Proposed(
        slug=m.slug,
        draft=m,
        clip=clip,
        issues=issues,
        report=report,
        rank=rank_score(clip.scores, rules) if clip else 0.0,
    )


def propose(
    rules: BrandRules,
    transcript: Transcript,
    draft: ProposalDraft,
    video: VideoContext | None = None,
    level: Level | None = None,
    faces: FaceProbe | None = None,
    spoilers: list[str] | None = None,
    repair=None,
) -> list[Proposed]:
    """Compile, validate, repair and rank a draft. `repair(failed) -> ProposalDraft` is called
    for each repair round with the failing drafts and their issues; None skips repairs."""
    ids: dict[str, int] = {}

    def unique_id(slug: str) -> str:
        base = f"{rules.proposals.clip_prefix}{_slug(slug)}"
        ids[base] = ids.get(base, 0) + 1
        return base if ids[base] == 1 else f"{base}-{ids[base]}"

    results = []
    for m in draft.moments:
        results.append(
            check_draft(m, transcript, rules, video, level, faces, spoilers, unique_id(m.slug))
        )

    for round_no in range(rules.proposals.max_repairs if repair else 0):
        failed = [r for r in results if not r.passed]
        if not failed:
            break
        fixed = repair([(r.draft, r.problems()) for r in failed])
        by_slug = {m.slug: m for m in fixed.moments}
        for i, r in enumerate(results):
            m = by_slug.get(r.slug)
            if r.passed or m is None:
                continue
            cid = r.clip.id if r.clip else f"{rules.proposals.clip_prefix}{_slug(r.slug)}"
            new = check_draft(m, transcript, rules, video, level, faces, spoilers, cid)
            new.attempts = round_no + 2
            results[i] = new

    results.sort(key=lambda r: (not r.passed, -r.rank))
    return results


def save_draft(draft: ProposalDraft, path) -> None:
    path.write_text(json.dumps(draft.model_dump(), indent=2))


# --------------------------------------------------------------------------- vs. human picks


def _length(ranges: list[tuple[float, float]]) -> float:
    return sum(b - a for a, b in ranges)


def _union(segments: list[ClipSegment]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for g in sorted(segments, key=lambda g: g.start):
        if out and g.start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], g.end))
        else:
            out.append((g.start, g.end))
    return out


def overlap(a: ClipProposal, b: ClipProposal) -> float:
    """Intersection over union of the source time two clips play (order ignored)."""
    ua, ub = _union(a.segments), _union(b.segments)
    inter = sum(max(0.0, min(x1, y1) - max(x0, y0)) for x0, x1 in ua for y0, y1 in ub)
    union = _length(ua) + _length(ub) - inter
    return round(inter / union, 3) if union > 0 else 0.0


class PickMatch(BaseModel):
    human: str
    auto: str | None
    overlap: float
    auto_rank: int | None  # 1-based position among passing auto clips


def match_picks(
    auto: list[ClipProposal], human: list[ClipProposal], threshold: float = 0.3
) -> tuple[list[PickMatch], float]:
    """For each human-picked clip, the best-overlapping auto clip. Returns the matches and
    recall: the share of human picks that some auto clip covers at >= threshold overlap.
    `auto` must be in rank order."""
    matches = []
    for h in human:
        best = max(((overlap(a, h), i, a) for i, a in enumerate(auto)), default=None,
                   key=lambda x: x[0])  # fmt: skip
        if best is None or best[0] == 0:
            matches.append(PickMatch(human=h.id, auto=None, overlap=0.0, auto_rank=None))
        else:
            matches.append(
                PickMatch(human=h.id, auto=best[2].id, overlap=best[0], auto_rank=best[1] + 1)
            )
    hits = sum(m.overlap >= threshold for m in matches)
    return matches, round(hits / len(matches), 3) if matches else 0.0
