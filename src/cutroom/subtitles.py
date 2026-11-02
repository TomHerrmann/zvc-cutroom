"""Subtitles: Resolve AI transcription in, Claude accuracy review, corrected subtitles out.

Flow:
  1. In DaVinci Resolve Studio, generate subtitles from audio and export the track as SRT.
  2. `load_srt` turns it into a Transcript (cue-level timing).
  3. `review_with_claude` asks Claude for corrections only (misheard words, names,
     film titles), as a validated SubtitleReview object, never free text.
  4. `check_review` rejects corrections that don't line up with the real cues.
  5. `apply_review` + `write_srt` produce the corrected SRT that goes back into
     Resolve's subtitle track (burned in for vertical, uploaded as-is for wide).
"""

from __future__ import annotations

import re
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from .schemas import Issue, Segment, Severity, Transcript, Word
from .validators.text import normalize_tokens

if TYPE_CHECKING:  # pragma: no cover
    from .runlog import Run

# --------------------------------------------------------------------------- SRT


_SRT_TIME = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)")


def _srt_seconds(ts: str) -> float:
    m = _SRT_TIME.match(ts.strip())
    if not m:
        raise ValueError(f"bad SRT timestamp {ts!r}")
    h, mi, s, ms = m.groups()
    return int(h) * 3600 + int(mi) * 60 + int(s) + int(ms.ljust(3, "0")[:3]) / 1000


def _srt_time(t: float) -> str:
    ms = round(t * 1000)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _spread_words(text: str, start: float, end: float) -> list[Word]:
    toks = text.split()
    step = (end - start) / max(len(toks), 1)
    return [
        Word(text=t, start=round(start + i * step, 3), end=round(start + (i + 1) * step, 3))
        for i, t in enumerate(toks)
    ]


def load_srt(path: Path, video_id: str, duration: float) -> Transcript:
    """Import the subtitle track exported from Resolve. Cue ids are re-numbered from 0."""
    blocks = re.split(r"\n\s*\n", path.read_text(encoding="utf-8-sig").strip())
    segments: list[Segment] = []
    for block in blocks:
        lines = [ln for ln in block.splitlines() if ln.strip()]
        arrow = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if arrow is None:
            continue
        a, b = lines[arrow].split("-->")
        start, end = _srt_seconds(a), _srt_seconds(b)
        text = " ".join(lines[arrow + 1 :])
        segments.append(
            Segment(
                id=len(segments),
                start=start,
                end=end,
                text=text,
                words=_spread_words(text, start, end),
            )
        )
    return Transcript(
        video_id=video_id,
        source="resolve",
        duration=duration,
        model="resolve-ai-subtitles",
        word_timing="estimated",
        segments=segments,
    )


def write_srt(transcript: Transcript, path: Path) -> Path:
    """Plain SRT: what YouTube takes for wide videos and what Resolve re-imports."""
    path.parent.mkdir(parents=True, exist_ok=True)
    out = [
        f"{i}\n{_srt_time(s.start)} --> {_srt_time(s.end)}\n{s.text}\n"
        for i, s in enumerate(transcript.segments, start=1)
    ]
    path.write_text("\n".join(out), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- review


class SubtitleCorrection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cue_id: int = Field(description="The [id] of the cue being corrected.")
    original: str = Field(description="The cue's full text exactly as given.")
    corrected: str = Field(description="The cue's full corrected text.")
    kind: Literal["misheard", "proper_noun", "film_title", "punctuation", "other"]
    reason: str = Field(description="One short sentence on why.")


class UnverifiedPhrase(BaseModel):
    """A word or name the reviewer could not verify. Never auto-applied: a person decides."""

    model_config = ConfigDict(extra="forbid")
    cue_id: int
    phrase: str = Field(description="The exact words from the cue that can't be verified.")
    best_guess: str | None = Field(description="What it most likely should be, if anything.")
    reason: str = Field(description="One short sentence on why it can't be verified.")


class SubtitleReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    corrections: list[SubtitleCorrection]
    unverified: list[UnverifiedPhrase] = Field(default_factory=list)


class HumanAnswer(BaseModel):
    """A person's ruling on an unverified phrase. `replacement` equal to `phrase` keeps it."""

    model_config = ConfigDict(extra="forbid")
    cue_id: int
    phrase: str
    replacement: str


REVIEW_SYSTEM = """\
You proofread auto-generated subtitles for Zero Vision Cinema, a film channel. \
The subtitles came from speech recognition, so expect misheard words, mangled names of \
actors and directors, and wrong film titles.

Return only cues that need a change. Copy `original` exactly as the cue appears. \
Write `corrected` as the full replacement text for that cue. Fix what the speaker \
actually said; do not reword, tidy up grammar, censor, or shorten. Keep corrections \
inside one cue; never move words between cues.

Only correct what you can verify from the surrounding cues or the glossary. If a word \
or name looks wrong but you can't verify the right one, do not change it: list it under \
`unverified` with the exact words from the cue, your best guess, and why. A person will \
decide."""


def _review_prompt(transcript: Transcript, glossary: list[str]) -> str:
    cues = "\n".join(f"[{s.id}] {s.text}" for s in transcript.segments)
    gloss = (
        "Names and titles that appear in this video (spell them exactly like this):\n"
        + "\n".join(f"- {g}" for g in glossary)
        + "\n\n"
        if glossary
        else ""
    )
    return f"{gloss}Subtitle cues:\n{cues}"


def review_with_claude(
    transcript: Transcript,
    model: str,
    glossary: list[str] | None = None,
    run: Run | None = None,
    client=None,
) -> SubtitleReview:
    import anthropic

    client = client or anthropic.Anthropic()
    response = client.beta.messages.parse(
        model=model,
        max_tokens=16000,
        system=REVIEW_SYSTEM,
        messages=[{"role": "user", "content": _review_prompt(transcript, glossary or [])}],
        output_format=SubtitleReview,
        output_config={"effort": "medium"},
        # On a safety decline, the API re-runs the request on a fallback model.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if run is not None:
        run.add_usage(response.model, response.usage.input_tokens, response.usage.output_tokens)
    if response.stop_reason == "refusal":
        raise RuntimeError("subtitle review was declined by the model")
    if response.stop_reason == "max_tokens" or response.parsed_output is None:
        raise RuntimeError(f"subtitle review incomplete (stop_reason={response.stop_reason})")
    return response.parsed_output


def check_review(transcript: Transcript, review: SubtitleReview) -> list[Issue]:
    """Validate a review before it touches the subtitles."""
    cues = {s.id: s for s in transcript.segments}
    counts = Counter(c.cue_id for c in review.corrections)
    issues: list[Issue] = []
    for i, c in enumerate(review.corrections):
        field = f"corrections[{i}]"
        cue = cues.get(c.cue_id)
        if cue is None:
            issues.append(Issue(code="UNKNOWN_CUE", field=field, message=f"no cue {c.cue_id}"))
            continue
        if counts[c.cue_id] > 1:
            issues.append(
                Issue(code="DUPLICATE_CUE", field=field, message=f"cue {c.cue_id} corrected twice")
            )
        if c.original.strip() != cue.text.strip():
            issues.append(
                Issue(
                    code="ORIGINAL_MISMATCH",
                    field=field,
                    message=f"cue {c.cue_id} says {cue.text!r}, review claims {c.original!r}",
                )
            )
        if not c.corrected.strip():
            issues.append(Issue(code="EMPTY_CORRECTION", field=field, message="empty text"))
        elif c.corrected.strip() == cue.text.strip():
            issues.append(
                Issue(code="NO_OP", field=field, severity=Severity.WARNING, message="no change")
            )
        else:
            a, b = normalize_tokens(cue.text), normalize_tokens(c.corrected)
            ratio = SequenceMatcher(a=a, b=b, autojunk=False).ratio()
            if ratio < 0.5:
                issues.append(
                    Issue(
                        code="LARGE_EDIT",
                        field=field,
                        severity=Severity.WARNING,
                        message=f"cue {c.cue_id} rewritten heavily (similarity {ratio:.2f}); "
                        "check by ear",
                    )
                )
    return issues


def apply_review(transcript: Transcript, review: SubtitleReview) -> tuple[Transcript, list[Issue]]:
    """Apply every correction whose cue has no error. Timing never changes."""
    issues = check_review(transcript, review)
    bad = {
        review.corrections[int(i.field.split("[")[1].rstrip("]"))].cue_id
        for i in issues
        if i.severity == Severity.ERROR and i.field
    }
    fixes = {c.cue_id: c.corrected.strip() for c in review.corrections if c.cue_id not in bad}
    segments = [
        s.model_copy(
            update={"text": fixes[s.id], "words": _spread_words(fixes[s.id], s.start, s.end)}
        )
        if s.id in fixes
        else s
        for s in transcript.segments
    ]
    out = transcript.model_copy(update={"segments": segments, "source": "resolve_reviewed"})
    return out, issues


# --------------------------------------------------------------------------- unverified


def check_unverified(transcript: Transcript, review: SubtitleReview) -> list[Issue]:
    cues = {s.id: s for s in transcript.segments}
    issues: list[Issue] = []
    for i, f in enumerate(review.unverified):
        cue = cues.get(f.cue_id)
        if cue is None or f.phrase not in cue.text:
            issues.append(
                Issue(
                    code="FLAG_NOT_IN_CUE",
                    field=f"unverified[{i}]",
                    message=f"{f.phrase!r} is not in cue {f.cue_id}",
                )
            )
    return issues


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def flag_context(transcript: Transcript, cue_id: int, phrase: str) -> dict[str, str | float]:
    """The sentence before, the sentence containing the phrase, and the sentence after."""
    full, offset = "", None
    cue_start = 0.0
    for s in transcript.segments:
        if s.id == cue_id:
            offset = len(full) + s.text.find(phrase)
            cue_start = s.start
        full += s.text + " "
    if offset is None:
        raise KeyError(cue_id)
    sentences, pos = [], 0
    for part in _SENTENCE_END.split(full.strip()):
        start = full.find(part, pos)
        sentences.append((start, start + len(part), part))
        pos = start + len(part)
    idx = next(i for i, (a, b, _) in enumerate(sentences) if a <= offset < b)
    get = lambda i: sentences[i][2] if 0 <= i < len(sentences) else ""  # noqa: E731
    return {"time": cue_start, "before": get(idx - 1), "sentence": get(idx), "after": get(idx + 1)}


def questions_markdown(transcript: Transcript, review: SubtitleReview) -> str:
    lines = [
        "# Subtitle questions",
        "",
        "These words couldn't be verified, so they were left as is.",
        "Answer in `subtitle_answers.yaml` and rerun with `--answers`.",
        "",
    ]
    for f in review.unverified:
        try:
            ctx = flag_context(transcript, f.cue_id, f.phrase)
        except (KeyError, StopIteration):
            continue
        m, s = divmod(int(ctx["time"]), 60)
        lines += [
            f'## {m}:{s:02d}  "{f.phrase}"  (cue {f.cue_id})',
            "",
            f"> **Before:** {ctx['before']}",
            f"> **Sentence:** {ctx['sentence']}",
            f"> **After:** {ctx['after']}",
            "",
            f"Best guess: {f.best_guess or 'none'}. {f.reason}",
            "",
        ]
    return "\n".join(lines)


def apply_answers(
    transcript: Transcript, answers: list[HumanAnswer]
) -> tuple[Transcript, list[Issue]]:
    """Apply a person's rulings. Each phrase must appear exactly once in its cue."""
    cues = {s.id: s for s in transcript.segments}
    issues: list[Issue] = []
    new_text: dict[int, str] = {}
    for i, a in enumerate(answers):
        text = new_text.get(a.cue_id, cues[a.cue_id].text if a.cue_id in cues else None)
        if text is None or text.count(a.phrase) != 1:
            issues.append(
                Issue(
                    code="ANSWER_NOT_APPLIED",
                    field=f"answers[{i}]",
                    message=f"{a.phrase!r} not found exactly once in cue {a.cue_id}",
                )
            )
            continue
        new_text[a.cue_id] = text.replace(a.phrase, a.replacement)
    segments = [
        s.model_copy(
            update={"text": new_text[s.id], "words": _spread_words(new_text[s.id], s.start, s.end)}
        )
        if s.id in new_text
        else s
        for s in transcript.segments
    ]
    return transcript.model_copy(update={"segments": segments}), issues


# --------------------------------------------------------------------------- per-clip


def clip_transcript(transcript: Transcript, clip) -> Transcript:
    """Re-time the cues that fall inside a clip's segments onto the clip's own timeline."""
    segments: list[Segment] = []
    offset = 0.0
    for g in clip.segments:
        for s in transcript.segments:
            overlap = min(s.end, g.end) - max(s.start, g.start)
            # keep a cue only if most of it is actually in the cut
            if overlap <= 0 or overlap < 0.5 * (s.end - s.start):
                continue
            a = max(s.start, g.start) - g.start + offset
            b = min(s.end, g.end) - g.start + offset
            if transcript.word_timing == "estimated":
                words = _spread_words(s.text, round(a, 3), round(b, 3))
            else:  # real word times: shift them, keep the words whose middle is in the cut
                words = [
                    w.model_copy(
                        update={
                            "start": round(max(w.start, g.start) - g.start + offset, 3),
                            "end": round(min(w.end, g.end) - g.start + offset, 3),
                        }
                    )
                    for w in s.words
                    if g.start <= (w.start + w.end) / 2 <= g.end
                ]
            segments.append(
                Segment(
                    id=len(segments),
                    start=round(a, 3),
                    end=round(b, 3),
                    text=s.text,
                    words=words,
                )
            )
        offset += g.duration
    return transcript.model_copy(
        update={
            "video_id": f"{clip.video_id}/{clip.id}",
            "duration": round(offset, 3),
            "segments": segments,
        }
    )
