"""Typed data model for the whole pipeline.

Every stage reads and writes these objects. Nothing downstream ever consumes
free-form model text: LLM output is parsed into these models and anything that
does not parse is rejected before a validator ever sees it.

Times are always seconds as floats, measured from the start of the source video
unless a field says otherwise.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- transcript


class Word(_Model):
    text: str
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    confidence: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def _ordered(self) -> Word:
        if self.end < self.start:
            raise ValueError(f"word {self.text!r} ends before it starts")
        return self


class Segment(_Model):
    id: int
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    text: str
    speaker: str | None = None
    words: list[Word] = Field(default_factory=list)


class Transcript(_Model):
    """A timed transcript of one source video."""

    video_id: str
    # resolve: DaVinci Resolve Studio AI subtitles. resolve_reviewed: after the Claude
    # accuracy pass has been applied.
    source: Literal["resolve", "resolve_reviewed", "manual", "fixture"]
    language: str = "en"
    duration: float = Field(gt=0)
    model: str | None = None
    # SRT cues carry no per-word timing, so word times inside a cue are spread evenly
    # ("estimated"). Quote lookups then match at cue level instead of word level.
    word_timing: Literal["exact", "estimated"] = "exact"
    segments: list[Segment]

    def words(self) -> list[Word]:
        return [w for s in self.segments for w in s.words]

    def text_between(self, start: float, end: float) -> str:
        return " ".join(w.text for w in self.words() if w.start >= start and w.end <= end)


# --------------------------------------------------------------------------- clips


class Format(StrEnum):
    VERTICAL = "vertical"  # 1080x1920, short form, TikTok, burned-in captions
    WIDE = "wide"  # 3840x2160, long form, YouTube, SRT sidecar


class CaptionMode(StrEnum):
    BURNED_IN = "burned_in"
    SRT = "srt"


class QuoteRef(_Model):
    """A verbatim quote with the timestamp where it is claimed to be spoken."""

    text: str = Field(min_length=1)
    start: float = Field(ge=0)
    end: float = Field(ge=0)


class CTA(_Model):
    text: str = Field(min_length=1)
    kind: Literal["spoken", "overlay"]
    # Seconds from the start of the *clip*, not the source video.
    at: float = Field(ge=0)


class CoverCandidate(_Model):
    frame_time: float = Field(ge=0, description="Source-video time of the frame.")
    overlay_text: str
    # None means nobody (human or detector) has checked yet. Unverified fails the check.
    has_face: bool | None = None
    selected: bool = False


class MomentScores(_Model):
    """Per-signal scores in [0, 1]. Filled by the ranker in milestone 2."""

    hook: float | None = Field(default=None, ge=0, le=1)
    face_on_screen: float | None = Field(default=None, ge=0, le=1)
    quote_punch: float | None = Field(default=None, ge=0, le=1)
    length_fit: float | None = Field(default=None, ge=0, le=1)
    theme: float | None = Field(default=None, ge=0, le=1)


class ClipSegment(_Model):
    """One continuous range of the source video. A clip is one or more of these, played in order."""

    start: float = Field(ge=0, description="Source-video seconds.")
    end: float = Field(gt=0, description="Source-video seconds.")

    @model_validator(mode="after")
    def _ordered(self) -> ClipSegment:
        if self.end <= self.start:
            raise ValueError(f"segment ends ({self.end}) before it starts ({self.start})")
        return self

    @property
    def duration(self) -> float:
        return self.end - self.start


class VideoContext(_Model):
    """Per-video facts the validators need beyond the transcript."""

    video_id: str
    # Measured audio silences (start, end), e.g. from ffmpeg silencedetect. Segment edges
    # must land inside one, so no clip starts or ends mid-word.
    silences: list[tuple[float, float]] = Field(default_factory=list)
    # Ranges that only belong in the long-form YouTube video (title card, sponsor reads).
    youtube_only: list[tuple[float, float]] = Field(default_factory=list)
    # Where the longer YouTube review lives (URL or title). Set it and clips must end on
    # the end card pointing to it.
    long_review: str | None = None

    def in_silence(self, t: float, slack: float = 0.03) -> bool:
        return any(a - slack <= t <= b + slack for a, b in self.silences)

    def snap(self, t: float, window: float = 0.6) -> float:
        """Move t to the middle of the nearest silence within `window` seconds."""
        best = None
        for a, b in self.silences:
            mid = (a + b) / 2
            if abs(mid - t) <= window and (best is None or abs(mid - t) < abs(best - t)):
                best = mid
        return round(best, 3) if best is not None else t


class ClipStatus(StrEnum):
    PROPOSED = "proposed"
    REJECTED_BY_VALIDATOR = "rejected_by_validator"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED_BY_HUMAN = "rejected_by_human"
    POSTED = "posted"


class ClipProposal(_Model):
    """One proposed short clip. This is the edit decision that Resolve renders.

    It lives in the clip store, never only in a Resolve project.
    """

    id: str
    video_id: str
    format: Format = Format.VERTICAL
    # Played in order. A clip can stitch lines from different parts of the video.
    segments: list[ClipSegment] = Field(min_length=1)
    hook: str = Field(description="What the first seconds are selling, in one line.")
    caption: str
    hashtags: list[str] = Field(default_factory=list)
    title_variants: list[str] = Field(default_factory=list)
    quotes: list[QuoteRef] = Field(default_factory=list)
    cta: CTA | None = None
    covers: list[CoverCandidate] = Field(default_factory=list)
    # Topic Title text on the closing ZVC title card, e.g. "FULL VIDEO ON YOUTUBE".
    # Plays after the last segment; its length comes from the brand rules.
    end_card: str | None = None
    width: int = 1080
    height: int = 1920
    captions: CaptionMode = CaptionMode.BURNED_IN
    scores: MomentScores = Field(default_factory=MomentScores)
    status: ClipStatus = ClipStatus.PROPOSED
    created_by: Literal["human", "llm", "fixture"] = "human"

    @property
    def duration(self) -> float:
        return sum(seg.duration for seg in self.segments)

    def contains(self, start: float, end: float | None = None, slack: float = 0.0) -> bool:
        """True if source range [start, end] sits inside one segment."""
        end = start if end is None else end
        return any(g.start - slack <= start and end <= g.end + slack for g in self.segments)

    def source_time(self, clip_t: float) -> float:
        """Map a time in the edited clip to a time in the source video."""
        elapsed = 0.0
        for g in self.segments:
            if clip_t <= elapsed + g.duration:
                return g.start + (clip_t - elapsed)
            elapsed += g.duration
        return self.segments[-1].end

    def selected_cover(self) -> CoverCandidate | None:
        chosen = [c for c in self.covers if c.selected]
        return chosen[0] if chosen else None


# --------------------------------------------------------------------------- validation


class Severity(StrEnum):
    ERROR = "error"  # clip is rejected
    WARNING = "warning"  # clip passes but the reviewer sees the note


class Issue(_Model):
    code: str = Field(description="Stable machine code, e.g. QUOTE_NOT_IN_TRANSCRIPT.")
    message: str
    severity: Severity = Severity.ERROR
    field: str | None = None


class ValidationReport(_Model):
    clip_id: str
    issues: list[Issue] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not any(i.severity == Severity.ERROR for i in self.issues)

    @property
    def error_codes(self) -> list[str]:
        return sorted({i.code for i in self.issues if i.severity == Severity.ERROR})


# --------------------------------------------------------------------------- runs


def _now() -> datetime:
    return datetime.now(UTC)


class RunRecord(_Model):
    """One line in runs/runs.jsonl. Written for every pipeline or eval run."""

    run_id: str
    command: str
    started_at: datetime = Field(default_factory=_now)
    latency_ms: float = 0.0
    git_sha: str | None = None
    config_hash: str | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    n_items: int = 0
    n_passed: int = 0
    n_failed: int = 0
    failure_codes: dict[str, int] = Field(default_factory=dict)
    extra: dict[str, object] = Field(default_factory=dict)
    ok: bool = True
    error: str | None = None
