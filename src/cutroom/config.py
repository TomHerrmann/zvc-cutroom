"""Brand rules: loaded from YAML, validated with Pydantic, hashed for run logs."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .schemas import CaptionMode, Format

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
    # Set by load_rules; not part of the YAML.
    config_hash: str | None = None


def load_rules(path: Path | str | None = None) -> BrandRules:
    p = Path(path) if path else DEFAULT_RULES_PATH
    raw = p.read_bytes()
    rules = BrandRules.model_validate(yaml.safe_load(raw))
    rules.config_hash = hashlib.sha256(raw).hexdigest()[:12]
    return rules
