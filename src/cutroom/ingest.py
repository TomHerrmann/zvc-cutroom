"""Ingest: audio extraction, Whisper transcription, Resolve transcript import, cross-check.

Scene detection via Resolve's Neural Engine arrives with the Resolve integration
(milestone 3); it needs Resolve Studio running on the editing machine.
"""

from __future__ import annotations

import re
import subprocess
from difflib import SequenceMatcher
from pathlib import Path

from pydantic import BaseModel

from .schemas import Segment, Transcript, Word
from .validators.text import normalize_tokens


def extract_audio(video: Path, out: Path) -> Path:
    """16 kHz mono WAV, which is what Whisper wants."""
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(video),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(out),
        ],
        check=True,
    )
    return out


def probe_duration(media: Path) -> float:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(media),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(out.stdout.strip())


def transcribe_whisper(
    audio: Path,
    video_id: str,
    model_size: str = "large-v3",
    device: str = "auto",
    compute_type: str = "default",
) -> Transcript:
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:  # pragma: no cover
        raise SystemExit("Whisper is an optional extra: pip install 'cutroom[asr]'") from e

    model = WhisperModel(model_size, device=device, compute_type=compute_type)
    segs, info = model.transcribe(str(audio), word_timestamps=True, vad_filter=True)
    segments = [
        Segment(
            id=i,
            start=s.start,
            end=s.end,
            text=s.text.strip(),
            words=[
                Word(
                    text=w.word.strip(),
                    start=w.start,
                    end=max(w.end, w.start),
                    confidence=w.probability,
                )
                for w in (s.words or [])
            ],
        )
        for i, s in enumerate(segs)
    ]
    return Transcript(
        video_id=video_id,
        source="whisper",
        language=info.language,
        duration=info.duration,
        model=f"faster-whisper/{model_size}",
        segments=segments,
    )


_SRT_TIME = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)")


def _srt_seconds(ts: str) -> float:
    h, m, s, ms = _SRT_TIME.match(ts.strip()).groups()  # type: ignore[union-attr]
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")[:3]) / 1000


def load_srt(path: Path, video_id: str, duration: float, source: str = "resolve") -> Transcript:
    """Import subtitles exported from Resolve's transcription. SRT has no word timings,
    so words are spread evenly across each cue. Good enough for cross-checking text,
    not for quote timestamps; Whisper's word timings stay authoritative."""
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
        toks = text.split()
        step = (end - start) / max(len(toks), 1)
        words = [
            Word(text=t, start=start + i * step, end=start + (i + 1) * step)
            for i, t in enumerate(toks)
        ]
        segments.append(Segment(id=len(segments), start=start, end=end, text=text, words=words))
    return Transcript(
        video_id=video_id,
        source=source,
        duration=duration,  # type: ignore[arg-type]
        segments=segments,
    )


class Disagreement(BaseModel):
    start: float
    end: float
    reference: str
    other: str


class CrossCheck(BaseModel):
    reference_words: int
    wer: float
    disagreements: list[Disagreement]


def crosscheck(reference: Transcript, other: Transcript, min_words: int = 2) -> CrossCheck:
    """Word error rate of `other` against `reference`, plus the regions where they differ.

    Regions that disagree are exactly where a quote is most likely to be wrong, so the
    review UI flags any proposed quote that overlaps one.
    """
    ref_words = [(t, w) for w in reference.words() for t in normalize_tokens(w.text)]
    oth_words = [(t, w) for w in other.words() for t in normalize_tokens(w.text)]
    a = [t for t, _ in ref_words]
    b = [t for t, _ in oth_words]
    errors = 0
    diffs: list[Disagreement] = []
    for op, i1, i2, j1, j2 in SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        errors += max(i2 - i1, j2 - j1)
        if max(i2 - i1, j2 - j1) < min_words:
            continue
        span = ref_words[i1:i2] or ref_words[max(i1 - 1, 0) : i1 + 1]
        diffs.append(
            Disagreement(
                start=span[0][1].start if span else 0.0,
                end=span[-1][1].end if span else 0.0,
                reference=" ".join(a[i1:i2]),
                other=" ".join(b[j1:j2]),
            )
        )
    return CrossCheck(
        reference_words=len(a), wer=round(errors / max(len(a), 1), 4), disagreements=diffs
    )
