"""Audio level map for cut placement.

Word times (aligned or not) are only accurate to about a tenth of a second, which is
enough to clip a soft "A" or the tail of "there". Cut edges are therefore snapped to
measured silence: the 5 ms RMS level of the source audio.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

HOP = 0.005  # seconds per level frame
SILENT_DB = -55.0  # this channel's audio is gated; speech sits far above this


class Level:
    def __init__(self, db: np.ndarray):
        self.db = db

    @classmethod
    def from_wav(cls, path: Path) -> Level:
        with wave.open(str(path)) as w:
            sr = w.getframerate()
            x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
        x /= 32768.0
        hop = int(sr * HOP)
        n = len(x) // hop
        rms = np.sqrt((x[: n * hop].reshape(n, hop) ** 2).mean(axis=1) + 1e-12)
        return cls(20 * np.log10(rms))

    def at(self, t: float, win: float = 0.02) -> float:
        a, b = int((t - win / 2) / HOP), int((t + win / 2) / HOP) + 1
        seg = self.db[max(a, 0) : max(b, a + 1)]
        return float(seg.max()) if len(seg) else -120.0

    def silences(self, lo: float, hi: float, min_len: float = 0.02) -> list[tuple[float, float]]:
        a, b = max(int(lo / HOP), 0), min(int(hi / HOP), len(self.db))
        quiet = self.db[a:b] < SILENT_DB
        out, i = [], 0
        while i < len(quiet):
            if quiet[i]:
                j = i
                while j < len(quiet) and quiet[j]:
                    j += 1
                if (j - i) * HOP >= min_len:
                    out.append(((a + i) * HOP, (a + j) * HOP))
                i = j
            else:
                i += 1
        return out

    def cut_in(self, word_start: float, prev_end: float) -> tuple[float, bool]:
        """Cut point before a word: just before speech onset, inside real silence.
        Returns (time, clean) where clean=False means no silence was found."""
        quiet = self.silences(prev_end - 0.1, word_start + 0.12, 0.05) or self.silences(
            prev_end - 0.1, word_start + 0.12
        )
        if not quiet:
            lo, hi = int((prev_end - 0.05) / HOP), int((word_start + 0.05) / HOP)
            k = lo + int(np.argmin(self.db[lo:hi])) if hi > lo else int(word_start / HOP)
            return round(k * HOP, 3), False
        # nearest pause wins; a longer one (a real gap, not a consonant) gets a head start
        a, b = min(quiet, key=lambda s: abs(s[1] - word_start) - 0.5 * (s[1] - s[0]))
        return round(max(a, b - 0.025), 3), True

    def cut_out(self, word_end: float, next_start: float) -> tuple[float, bool]:
        """Cut point after a word: just after it fades, inside real silence."""
        quiet = self.silences(word_end - 0.12, next_start + 0.1, 0.05) or self.silences(
            word_end - 0.12, next_start + 0.1
        )
        if not quiet:
            lo, hi = int((word_end - 0.05) / HOP), int((next_start + 0.05) / HOP)
            k = lo + int(np.argmin(self.db[lo:hi])) if hi > lo else int(word_end / HOP)
            return round(k * HOP, 3), False
        a, b = min(quiet, key=lambda s: abs(s[0] - word_end) - 0.5 * (s[1] - s[0]))
        return round(min(b, a + 0.03), 3), True
