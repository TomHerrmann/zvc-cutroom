"""Per-video facts the validators and cut placement need: audio, silences, YouTube-only
ranges (the title card), and whether a longer review exists."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from pathlib import Path

from .audio import HOP, Level
from .schemas import VideoContext

Runner = Callable[..., subprocess.CompletedProcess]


def extract_audio(video: Path, wav: Path, runner: Runner = subprocess.run) -> Path:
    """16 kHz mono 16-bit WAV: what the aligner and the level map read."""
    wav.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(video),
           "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)]  # fmt: skip
    res = runner(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {res.stderr[-400:]}")
    return wav


def scene_changes(
    video: Path, seconds: float = 60, threshold: float = 0.4, runner: Runner = subprocess.run
) -> list[float]:
    """Hard visual cuts in the first `seconds`: a title card shows up as a pair of them."""
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-t", str(seconds), "-i", str(video),
           "-vf", f"select='gt(scene,{threshold})',showinfo", "-an", "-f", "null", "-"]  # fmt: skip
    res = runner(cmd, capture_output=True, text=True)
    return [round(float(t), 2) for t in re.findall(r"pts_time:([0-9.]+)", res.stderr)]


def build_context(
    video_id: str,
    wav: Path,
    youtube_only: list[tuple[float, float]] | None = None,
    long_review: str | None = None,
) -> VideoContext:
    level = Level.from_wav(wav)
    duration = len(level.db) * HOP
    silences = [(round(a, 3), round(b, 3)) for a, b in level.silences(0, duration, 0.04)]
    return VideoContext(
        video_id=video_id,
        silences=silences,
        youtube_only=youtube_only or [],
        long_review=long_review,
    )


def parse_range(text: str) -> tuple[float, float]:
    """'8.23-14.53' -> (8.23, 14.53)"""
    a, b = (float(x) for x in text.split("-", 1))
    if b <= a:
        raise ValueError(f"range {text!r} ends before it starts")
    return a, b
