"""Brand rules: loaded from YAML, validated with Pydantic, hashed for run logs."""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .schemas import Platform

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


class PlatformRules(_Cfg):
    aspect_ratio: str = "9:16"
    min_seconds: float
    max_seconds: float
    caption_max_chars: int


class BrandRules(_Cfg):
    brand: str
    version: int
    cta: CTARules
    cover: CoverRules
    quotes: QuoteRules
    spoilers: SpoilerRules
    hashtags: HashtagRules
    platforms: dict[Platform, PlatformRules]
    # Set by load_rules; not part of the YAML.
    config_hash: str | None = None


def load_rules(path: Path | str | None = None) -> BrandRules:
    p = Path(path) if path else DEFAULT_RULES_PATH
    raw = p.read_bytes()
    rules = BrandRules.model_validate(yaml.safe_load(raw))
    rules.config_hash = hashlib.sha256(raw).hexdigest()[:12]
    return rules
