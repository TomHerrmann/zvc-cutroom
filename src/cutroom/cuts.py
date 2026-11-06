"""Turn transcript lines into clip segments whose edges sit in real silence.

Both the hand-built pilot (`scripts/build_pilot_clips.py`) and the automated proposer
(`propose.py`) cut through here, so a line range always becomes the same cut.
"""

from __future__ import annotations

from collections.abc import Iterable

from .audio import Level
from .schemas import ClipSegment, Transcript, VideoContext

FRAME = 1 / 30  # one frame at the vertical delivery rate


class CutError(ValueError):
    """The lines asked for can't become a segment (unknown ids, no words, long-form only)."""

    def __init__(self, message: str, code: str = "UNKNOWN_LINE"):
        super().__init__(message)
        self.code = code


def _gap_cut(silences: list[tuple[float, float]], lo: float, hi: float, prefer: float) -> float:
    """A point inside a measured silence between lo and hi, as close to `prefer` as possible."""
    inside = [(max(a, lo), min(b, hi)) for a, b in silences if b > lo and a < hi]
    if not inside:
        return round((lo + hi) / 2, 3)
    a, b = min(inside, key=lambda s: min(abs(s[0] - prefer), abs(s[1] - prefer)))
    return round(min(max(prefer, a), b), 3)


def cut_lines(
    transcript: Transcript,
    line_ids: Iterable[int],
    level: Level | None = None,
    video: VideoContext | None = None,
) -> tuple[ClipSegment, bool]:
    """One segment holding exactly these lines, cut in the pauses either side of them.

    With the audio level map, edges snap into measured silence (the pilot's method). With only
    the video's silence list, they land in the silence nearest the words. Returns the segment
    and whether both edges found a clean pause.
    """
    ids = set(line_ids)
    lines = [c for c in transcript.segments if c.id in ids]
    if not lines or len(lines) != len(ids):
        missing = sorted(ids - {c.id for c in lines})
        raise CutError(f"unknown line ids {missing}")
    lines = [c for c in lines if c.words]
    if not lines:
        raise CutError(f"lines {sorted(ids)} have no words")
    words = transcript.words()
    w0, w1 = lines[0].words[0], lines[-1].words[-1]
    i0, i1 = words.index(w0), words.index(w1)
    prev_end = words[i0 - 1].end if i0 > 0 else 0.0
    next_start = words[i1 + 1].start if i1 + 1 < len(words) else transcript.duration

    if level is not None:
        start, ok_in = level.cut_in(w0.start, prev_end)
        end, ok_out = level.cut_out(w1.end, next_start)
    elif video is not None and video.silences:
        start = _gap_cut(video.silences, prev_end, w0.start, w0.start - 0.025)
        end = _gap_cut(video.silences, w1.end, next_start, w1.end + 0.03)
        ok_in, ok_out = video.in_silence(start), video.in_silence(end)
    else:
        start = max(prev_end, w0.start - 0.1)
        end = min(next_start, w1.end + 0.1)
        ok_in = ok_out = False

    if video is not None:  # never bleed into long-form-only material (the title card)
        for a, b in video.youtube_only:
            if a <= start and end <= b:
                raise CutError(f"lines {sorted(ids)} are long-form only", "LONG_FORM_ONLY_LINE")
            if start < a < end:
                end = a - FRAME
            if start < b < end:
                start = b + FRAME
    start, end = round(max(start, 0.0), 3), round(end, 3)
    if end <= start:
        raise CutError(f"lines {sorted(ids)} are long-form only", code="LONG_FORM_ONLY_LINE")
    return ClipSegment(start=start, end=end), ok_in and ok_out
