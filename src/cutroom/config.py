"""Brand rules: loaded from YAML, validated with Pydantic, hashed for run logs."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .schemas import CaptionMode, Format, MomentScores

DEFAULT_RULES_PATH = Path(__file__).resolve().parents[2] / "config" / "brand_rules.yaml"


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CTARules(_Cfg):
    required: bool = True
    max_start_seconds: float = 5.0
    allowed_kinds: list[str] = Field(default_factory=lambda: ["spoken", "overlay"])


class CoverRules(_Cfg):
    require_face: bool = True
    min_words: int = 3
    max_words: int = 5


class QuoteRules(_Cfg):
    timestamp_tolerance_seconds: float = 1.0
    match: str = "exact_normalized"


class SpoilerRules(_Cfg):
    banned_terms: list[str] = Field(default_factory=list)


class HashtagRules(_Cfg):
    required: list[str] = Field(default_factory=list)
    max_count: int = 5
    pattern: str = r"^#[A-Za-z0-9_]+$"


class RenderPreset(_Cfg):
    codec: str
    target_bitrate_mbps: float


class FormatRules(_Cfg):
    platform: str
    form: Literal["short", "long"]
    width: int
    height: int
    captions: CaptionMode
    min_seconds: float
    max_seconds: float
    caption_max_chars: int
    render: RenderPreset


class CaptionStyle(_Cfg):
    """Burned-in vertical captions (rendered by libass)."""

    font: str
    size: int = Field(gt=0)
    uppercase: bool = False
    max_words: int = Field(default=4, ge=1)
    outline: int = 6
    shadow: int = 0
    margin_x: int = 70
    margin_bottom: int = 560  # px from the bottom, clear of TikTok's caption/UI area
    hold: float = 0.35  # seconds a line stays up after its last word


class SubtitleRules(_Cfg):
    source: Literal["resolve"] = "resolve"
    review_model: str
    text_color: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    highlight_color: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    highlight: Literal["active_word", "none"] = "active_word"
    max_chars_per_line: dict[Format, int]
    caption_style: CaptionStyle


class EndCardRules(_Cfg):
    title: str  # name in Resolve's title library
    topic_title: str
    label: str | None = None
    seconds: float = Field(gt=0)
    # overlap: the card sits on a track above the video and starts overlap_frames before
    # the video ends, so it slams over the last moment instead of following a cut.
    transition_in: Literal["cut", "wipe", "overlap"] = "overlap"
    overlap_frames: int = Field(default=12, ge=0)
    only_when_long_review: bool = True
    # Spoken sign-offs the card replaces; a clip must not keep them.
    replaces: list[str] = Field(default_factory=list)


class ProposalCTA(_Cfg):
    text: str
    kind: Literal["spoken", "overlay"] = "overlay"
    at: float = Field(default=1.0, ge=0)


class ProposalRules(_Cfg):
    """Milestone 2: Claude scores moments and drafts clips; these rules shape the ask."""

    model: str = "claude-opus-5-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    count: int = Field(default=6, ge=1, le=20)  # moments to ask for
    max_segments: int = Field(default=3, ge=1)  # line ranges stitched into one clip
    max_repairs: int = Field(default=1, ge=0)  # rounds of "the checks said X, fix it"
    target_seconds: tuple[float, float] = (20.0, 45.0)  # sweet spot inside the format range
    cta: ProposalCTA = Field(default_factory=lambda: ProposalCTA(text="Follow Zero Vision Cinema"))
    # Ranking: weighted mean of the scores that are present.
    weights: dict[str, float] = Field(
        default_factory=lambda: {
            "hook": 0.3,
            "quote_punch": 0.25,
            "theme": 0.2,
            "length_fit": 0.15,
            "face_on_screen": 0.1,
        }
    )

    @model_validator(mode="after")
    def _known_weights(self) -> ProposalRules:
        unknown = set(self.weights) - set(MomentScores.model_fields)
        if unknown:
            raise ValueError(f"unknown score weights {sorted(unknown)}")
        lo, hi = self.target_seconds
        if not 0 < lo < hi:
            raise ValueError("target_seconds must be (low, high) with 0 < low < high")
        return self


PACE_KINDS = ("idle", "empty", "setup", "handwork", "repeat", "show")


class PacingRules(_Cfg):
    """Long-form pacing: what happens to the stretches with no talking.

    Spoken lines always play at real time. Each unspoken stretch is labelled with one kind
    (from the frames), and the kind sets its speed: 0 cuts it, 1 keeps it at real time.
    """

    model: str = "claude-opus-5-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    # Unspoken gaps shorter than this between kept lines are natural pauses and stay as shot.
    min_stretch_seconds: float = Field(default=2.0, gt=0)
    speeds: dict[str, float] = Field(
        default_factory=lambda: {
            "idle": 0,  # standing, thinking, repeating himself: cut
            "empty": 0,  # nobody in frame: cut
            "setup": 4,  # big moves: unboxing, carrying, plugging in
            "handwork": 4,  # hands on the projector or a reel: threading, fixing
            "repeat": 8,  # the same motion over and over: spooling, rewinding
            "show": 1,  # something to see with no words: first light, the image
        }
    )
    levels: list[float] = Field(default_factory=lambda: [1, 2, 4, 8])
    # A stretch this short is a beat between lines: never faster than short_max_speed.
    short_stretch_seconds: float = 5.0
    short_max_speed: float = 2
    # A long stretch steps up the levels until it plays in at most this long.
    max_output_seconds: float = 15.0
    # A sped piece that would play shorter than this is dropped (a flash, not a beat).
    min_output_seconds: float = 0.5
    # Inside an active stretch, a run this still for this long is cut (he left the frame).
    still_motion: float = 0.01
    still_min_seconds: float = 2.0
    sped_audio_volume: float = Field(default=0.5, ge=0, le=1)

    @model_validator(mode="after")
    def _known_kinds(self) -> PacingRules:
        if set(self.speeds) != set(PACE_KINDS):
            raise ValueError(f"pacing speeds must name exactly {list(PACE_KINDS)}")
        allowed = {0, *self.levels}
        bad = {k: v for k, v in self.speeds.items() if v not in allowed}
        if bad:
            raise ValueError(f"pacing speeds {bad} are not 0 or one of the levels {self.levels}")
        if sorted(self.levels) != self.levels or self.levels[0] != 1:
            raise ValueError("pacing levels must be ascending and start at 1")
        return self


class TitleCardRules(_Cfg):
    """The series title card: a Resolve timeline template (DRT) whose text is set per video."""

    template: str = "cutroom/templates/zerocontext-title.drt"
    text_tool: str = "LargeText"  # the Text+ tool in the template whose text changes
    seconds: float = Field(default=6.306, gt=0)  # 189 frames at 29.97
    max_chars: int = 34


class MusicTrack(_Cfg):
    """A music track cleared for use: free for commercial use, no attribution required."""

    file: str  # under the Zero Vision Cinema folder, or absolute
    title: str
    artist: str
    source_url: str
    license: str
    license_url: str | None = None


class MusicRules(_Cfg):
    """The music bed under sped-up work.

    Each track is first loudness-normalised to the cut's own loudness (`cutroom music --match`
    measures it from the review render; `track_lufs` is the fallback), so the levels below are
    relative to the talking whatever the mic gain was that day.
    """

    tracks: list[MusicTrack] = Field(default_factory=list)
    track_lufs: float = -20.0
    bed_db: float = Field(default=-5.0, le=0)  # under sped pieces
    duck_db: float = Field(default=-12.0, le=0)  # further down under real-time pieces in a run
    # Music is only for sped-up parts: by default any real-time piece (talking) ends the run.
    # Raise this to keep the music going, ducked, under lines up to this long between sped pieces.
    merge_gap_seconds: float = Field(default=0.0, ge=0)
    fade_in_seconds: float = Field(default=0.25, ge=0)
    fade_out_seconds: float = Field(default=0.4, ge=0)
    # How far the fade-out may run past the last sped piece; 0 means it is silent by the time
    # the talking starts.
    tail_seconds: float = Field(default=0.0, ge=0)
    duck_ramp_seconds: float = Field(default=0.25, ge=0)
    crossfade_seconds: float = Field(default=2.0, ge=0)  # between tracks in the playlist


class LongformRules(_Cfg):
    pacing: PacingRules = Field(default_factory=PacingRules)
    title_card: TitleCardRules = Field(default_factory=TitleCardRules)
    music: MusicRules = Field(default_factory=MusicRules)


class BrandRules(_Cfg):
    brand: str
    version: int
    cta: CTARules
    cover: CoverRules
    quotes: QuoteRules
    spoilers: SpoilerRules
    hashtags: HashtagRules
    formats: dict[Format, FormatRules]
    subtitles: SubtitleRules
    end_card: EndCardRules | None = None
    proposals: ProposalRules = Field(default_factory=ProposalRules)
    longform: LongformRules = Field(default_factory=LongformRules)
    # Set by load_rules; not part of the YAML.
    config_hash: str | None = None


def load_rules(path: Path | str | None = None) -> BrandRules:
    p = Path(path) if path else DEFAULT_RULES_PATH
    raw = p.read_bytes()
    rules = BrandRules.model_validate(yaml.safe_load(raw))
    rules.config_hash = hashlib.sha256(raw).hexdigest()[:12]
    return rules
