"""Word timing for a reviewed transcript by forced alignment.

Resolve's AI subtitles give one time range per line, not per word. Highlighting the
spoken word and cutting between words both need real word times, so this aligns the
already-reviewed text against the audio. It transcribes nothing: the words are fixed
and only their times are found.

Uses pocketsphinx's bundled US English model (the `align` extra), so nothing is
downloaded at run time. Audio must be 16 kHz mono 16-bit PCM WAV.
"""

from __future__ import annotations

import re
import wave
from pathlib import Path

from .schemas import Segment, Transcript, Word

# Spoken form of tokens the dictionary can't read as written.
SPOKEN = {
    "20": "twenty",
    "2026": "twenty twenty six",
    "28th": "twenty eighth",
    "phd": "p h d",
}
_LETTER = {
    "a": "AE", "b": "B", "c": "K", "d": "D", "e": "EH", "f": "F", "g": "G", "h": "HH",
    "i": "IH", "j": "JH", "k": "K", "l": "L", "m": "M", "n": "N", "o": "AA", "p": "P",
    "q": "K", "r": "R", "s": "S", "t": "T", "u": "AH", "v": "V", "w": "W", "x": "K S",
    "y": "Y", "z": "Z",
}  # fmt: skip
_TOKEN = re.compile(r"[A-Za-z0-9']+")


_ONES = (
    "zero one two three four five six seven eight nine ten eleven twelve thirteen "
    "fourteen fifteen sixteen seventeen eighteen nineteen"
).split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()
# ordinal suffixes are dropped ("21st" -> "twenty one"), close enough to align against
_UNITS = {"mm": "millimeter", "st": "", "nd": "", "rd": "", "th": ""}
_NUM = re.compile(r"(\d+)([a-z]*)$")


def _say_number(n: int) -> str:
    """0-9999 the way people say it: 16 -> 'sixteen', 1983 -> 'nineteen eighty three'."""
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + ("" if n % 10 == 0 else " " + _ONES[n % 10])
    if n < 1000:
        rest = "" if n % 100 == 0 else " " + _say_number(n % 100)
        return f"{_ONES[n // 100]} hundred{rest}"
    if n % 1000 < 10 or n >= 10000:  # 2005 -> "two thousand five"; long numbers digit by digit
        if n >= 10000:
            return " ".join(_ONES[int(c)] for c in str(n))
        rest = "" if n % 1000 == 0 else " " + _say_number(n % 1000)
        return f"{_ONES[n // 1000]} thousand{rest}"
    return f"{_say_number(n // 100)} {_say_number(n % 100)}"


def spoken(display_word: str) -> list[str]:
    """'post-Roe,' -> ['post', 'roe']; '2026' -> ['twenty', 'twenty', 'six'];
    '16mm' -> ['sixteen', 'millimeter']."""
    out: list[str] = []
    for tok in _TOKEN.findall(display_word.replace("-", " ")):
        tok = tok.lower().strip("'")
        if not tok:
            continue
        if tok in SPOKEN:
            out += SPOKEN[tok].split()
            continue
        m = _NUM.match(tok)
        if m:  # a word with no letters has no pronunciation and crashes the decoder
            out += _say_number(int(m.group(1))).split()
            if m.group(2):
                out += _UNITS.get(m.group(2), m.group(2)).split()
            continue
        out.append(tok)
    return out


class _Aligner:
    def __init__(self, wav: Path):
        from pocketsphinx import Decoder  # optional dependency

        with wave.open(str(wav)) as w:
            if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (16000, 1, 2):
                raise ValueError(f"{wav}: need 16 kHz mono 16-bit PCM")
            self.raw = w.readframes(w.getnframes())
            self.duration = w.getnframes() / 16000
        self.d = Decoder(bestpath=False, beam=1e-60, wbeam=1e-40, pbeam=1e-60, loglevel="ERROR")

    def add_words(self, tokens: set[str]) -> None:
        for tok in tokens:
            if not self.d.lookup_word(tok):
                self.d.add_word(tok, self._guess(tok), False)

    def _guess(self, tok: str) -> str:
        # Out-of-dictionary word: two known halves ("psycho"+"sexual"), else letters.
        for i in range(len(tok) - 2, 1, -1):
            a, b = self.d.lookup_word(tok[:i]), self.d.lookup_word(tok[i:])
            if a and b:
                return f"{a} {b}"
        # never an empty pronunciation: pocketsphinx segfaults on one
        return " ".join(_LETTER[c] for c in tok if c in _LETTER) or "AH"

    def run(self, tokens: list[str], a: float, b: float) -> list[tuple[float, float]] | None:
        """Times for each token in [a, b], or None if the decoder can't fit them."""
        if not tokens or b - a < 0.2:
            return [] if not tokens else None
        self.d.set_align_text(" ".join(tokens))
        self.d.start_utt()
        self.d.process_raw(self.raw[int(a * 16000) * 2 : int(b * 16000) * 2], full_utt=True)
        self.d.end_utt()
        seg = self.d.seg()
        if seg is None:
            return None
        times = [
            (round(a + s.start_frame / 100, 3), round(a + (s.end_frame + 1) / 100, 3))
            for s in seg
            if s.word != "<sil>" and not s.word.startswith(("[", "<"))
        ]
        if len(times) != len(tokens):
            return None
        # A word pressed against the window edge means the chunk cut through speech
        # (SRT line times are estimates): reject so the caller retries with neighbours.
        if times[-1][1] >= b - 0.02 and b < self.duration - 0.05:
            return None
        if times[0][0] <= a + 0.01 and a > 0.05:
            return None
        return times


def _chunks(
    transcript: Transcript, silences: list[tuple[float, float]], duration: float
) -> list[tuple[int, int, float, float]]:
    """Split at real pauses near line boundaries: (first_cue, end_cue, start_s, end_s)."""
    segs = transcript.segments
    out: list[tuple[int, int, float, float]] = []
    first, start = 0, 0.0
    for i in range(1, len(segs)):
        lo, hi = segs[i - 1].end - 0.4, segs[i].start + 0.4
        gaps = [(e - s, s, e) for s, e in silences if s < hi and e > lo]
        if not gaps:
            continue
        length, s, e = max(gaps)
        at = (s + e) / 2
        if length >= 0.2 and at > start + 0.3:
            out.append((first, i, start, at))
            first, start = i, at
    out.append((first, len(segs), start, duration))
    return out


def reject_stretched(
    new_segs: list[Segment], orig: list[Segment]
) -> tuple[list[Segment], list[int]]:
    """Over long non-speech stretches (a projector running, minutes of silence) the decoder can
    spread a line's words across the whole gap. A line whose words now span far more time than
    its subtitle cue is a bad fit: it goes back to its estimated times."""
    by_id = {s.id: s for s in orig}
    out, bad = [], []
    for s in new_segs:
        cue = by_id[s.id]
        span, length = s.end - s.start, cue.end - cue.start
        if s is not cue and span > max(1.5 * length, length + 1.5):
            bad.append(s.id)
            s = cue
        out.append(s)
    return out, bad


def align_transcript(
    transcript: Transcript, wav: Path, silences: list[tuple[float, float]]
) -> tuple[Transcript, list[int]]:
    """Return the transcript with aligned word times, plus the ids of any lines that could
    not be aligned (those keep their estimated times)."""
    al = _Aligner(wav)
    segs = transcript.segments
    display = [s.text.split() for s in segs]
    tokens = [[spoken(w) for w in words] for words in display]
    al.add_words({t for line in tokens for word in line for t in word})

    def flat(c0: int, c1: int) -> list[str]:
        return [t for line in tokens[c0:c1] for word in line for t in word]

    chunks = _chunks(transcript, silences, al.duration)
    result: dict[int, list[tuple[float, float]] | None] = {}
    for k, (c0, c1, a, b) in enumerate(chunks):
        result[k] = al.run(flat(c0, c1), a, b)
    # Lines whose SRT times are off can land in the wrong chunk; retry with neighbours.
    for j in [k for k, r in result.items() if r is None]:
        if result[j] is not None:
            continue
        for lo, hi, pad in [(j, j + 1, 0), (j - 1, j, 0), (j - 1, j + 1, 0), (j, j, 0.25),
                            (j - 1, j + 1, 0.25), (j - 2, j + 2, 0), (j - 2, j + 2, 0.25),
                            (j - 3, j + 3, 0), (j - 3, j + 3, 0.25)]:  # fmt: skip
            lo, hi = max(lo, 0), min(hi, len(chunks) - 1)
            c0, a = chunks[lo][0], chunks[lo][2]
            c1, b = chunks[hi][1], chunks[hi][3]
            r = al.run(flat(c0, c1), max(a - pad, 0), min(b + pad, al.duration))
            if r is not None:
                result[lo] = r
                for m in range(lo + 1, hi + 1):
                    result[m] = []
                break

    # Walk chunks back onto lines and display words.
    new_segs, failed = [], []
    for k, (c0, c1, _, _) in enumerate(chunks):
        times = result.get(k)
        if times == [] and k > 0:
            continue  # merged into an earlier chunk
        if times is None:
            failed += [segs[i].id for i in range(c0, c1)]
            new_segs += segs[c0:c1]
            continue
        # a merged result covers the following chunks' lines too
        end_cue = c1
        m = k + 1
        while m < len(chunks) and result.get(m) == []:
            end_cue = chunks[m][1]
            m += 1
        it = iter(times)
        for i in range(c0, end_cue):
            words = []
            for disp, toks in zip(display[i], tokens[i], strict=True):
                ts = [next(it) for _ in toks]
                if ts:
                    words.append(Word(text=disp, start=ts[0][0], end=ts[-1][1]))
                elif words:  # punctuation-only token: attach to the previous word
                    words[-1] = words[-1].model_copy(update={"text": words[-1].text + disp})
            s = segs[i]
            new_segs.append(
                s.model_copy(update={"words": words, "start": words[0].start, "end": words[-1].end})
                if words
                else s
            )
    checked, stretched = reject_stretched(new_segs, segs)
    aligned = transcript.model_copy(update={"segments": checked, "word_timing": "aligned"})
    failed += stretched
    return aligned, sorted(set(failed))
