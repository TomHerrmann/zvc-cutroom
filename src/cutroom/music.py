"""Background music under the sped-up parts of a long-form edit.

Sped-up work (threading, spooling, unboxing) plays with its own sound pitch-corrected and
lowered, which on its own sounds thin. This module lays a music bed under it:

  1. `layout` places every piece of an `EditPlan` on the cut's timeline, frame-rounded the same
     way `render_script` cuts them, with the title card as its own piece when it is included.
  2. `music_cues` groups back-to-back sped pieces into runs; music is for the sped-up parts
     only, so by default any talking ends a run (`merge_gap_seconds` can keep it going, ducked,
     under short lines). Each run is a list of (time, dB) points: a fade in over the end of the
     line before its first sped piece and a fade out that starts once its last one ends, so the
     music leads out of and back into the talking. A run that is too short in total
     (`min_run_seconds`) gets no music: it would only pop on and off.
  3. `build_bed` decodes the tracks (loudness-normalised), plays them as one continuous
     playlist that only advances while a run is playing, so every run picks up where the last
     one stopped, multiplies by the envelope and writes one WAV the length of the cut. Silence
     everywhere else, so it can sit on its own track (A2 in Resolve) from frame 0.

Only tracks whose license allows commercial use with no attribution go in the brand rules;
each carries its source page and license, and `credits` writes them next to the bed.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from .config import MusicRules, MusicTrack
from .pacing import EditPlan

SR = 48000
SILENT_DB = -120.0


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Piece(_Out):
    index: int = Field(description="Segment index in the plan, or -1 for the title card.")
    start: float
    end: float
    sped: bool
    kind: str


class Cue(_Out):
    """One run of music: gain points (seconds on the cut's timeline, dB) from start to end."""

    start: float
    end: float
    points: list[tuple[float, float]]
    segments: list[int] = Field(description="The sped segments this run plays under.")

    @property
    def duration(self) -> float:
        return self.end - self.start


def layout(plan: EditPlan, fps: int = 30, include_title: bool = True) -> list[Piece]:
    """Every piece on the cut's timeline, each a whole number of frames like render.sh."""
    out, t = [], 0.0
    card = plan.title_card
    for i, s in enumerate(plan.segments):
        if card and include_title and i == card.before_segment:
            n = round(card.seconds * fps)
            out.append(Piece(index=-1, start=t, end=t + n / fps, sped=False, kind="title"))
            t += n / fps
        n = max(round(s.out_duration * fps), 1)
        out.append(Piece(index=i, start=t, end=t + n / fps, sped=s.speed != 1, kind=s.kind))
        t += n / fps
    return out


def music_cues(pieces: Sequence[Piece], rules: MusicRules) -> list[Cue]:
    runs: list[list[Piece]] = []
    cur: list[Piece] = []
    held: list[Piece] = []  # real-time pieces since the last sped one
    for p in pieces:
        if p.sped:
            if cur and sum(h.end - h.start for h in held) <= rules.merge_gap_seconds:
                cur.extend(held)
            elif cur:
                runs.append(cur)
                cur = []
            cur.append(p)
            held = []
        elif p.kind == "title":
            if cur:
                runs.append(cur)
            cur, held = [], []
        elif cur:
            held.append(p)
    if cur:
        runs.append(cur)
    runs = [r for r in runs if sum(p.end - p.start for p in r if p.sped) >= rules.min_run_seconds]
    end_of_cut = pieces[-1].end if pieces else 0.0
    cues: list[Cue] = []
    for run in runs:
        first, last = run[0].start, run[-1].end
        nxt = [p.start for p in pieces if p.kind == "title" and p.start >= last]
        stop = min([last + rules.tail_seconds, end_of_cut, *nxt])
        bed, duck = rules.bed_db, rules.bed_db + rules.duck_db
        # Lead in over the line before, never into a title card or the previous cue.
        prev = [p.end for p in pieces if p.kind == "title" and p.end <= first]
        start = max([first - rules.lead_in_seconds, 0.0, *prev, *(c.end for c in cues[-1:])])
        if start < first:
            pts = [(start, SILENT_DB), (first, bed)]
        else:
            pts = [(first, SILENT_DB), (min(first + rules.fade_in_seconds, last), bed)]
        for p in run[1:]:
            if p.sped == (pts[-1][1] == bed):
                continue
            lvl = bed if p.sped else duck
            # the ramp happens just after the boundary, never before the previous point
            a = max(p.start, pts[-1][0])
            pts += [(a, pts[-1][1]), (min(a + rules.duck_ramp_seconds, p.end), lvl)]
        # The fade finishes by the end of the last sped piece plus any allowed tail (never past
        # the title card or the end of the video).
        fade_from = max(min(last, stop - rules.fade_out_seconds), pts[-1][0])
        pts += [(fade_from, pts[-1][1]), (stop, SILENT_DB)]
        cues.append(
            Cue(
                start=round(start, 4),
                end=round(stop, 4),
                points=[(round(t, 4), round(db, 2)) for t, db in pts],
                segments=[p.index for p in run if p.sped],
            )
        )
    return cues


def envelope(cue: Cue, sr: int = SR) -> np.ndarray:
    """Linear gain per sample across the cue, interpolated in dB between its points."""
    n = max(round(cue.duration * sr), 1)
    t = cue.start + np.arange(n) / sr
    ts, dbs = zip(*cue.points, strict=True)
    db = np.interp(t, ts, dbs)
    return np.where(db <= SILENT_DB + 1e-6, 0.0, 10 ** (db / 20)).astype(np.float32)


Decoder = Callable[[Path, float], np.ndarray]


def decode(path: Path, lufs: float, sr: int = SR) -> np.ndarray:
    """A track as float32 stereo at `sr`, loudness-normalised to `lufs` (ffmpeg loudnorm)."""
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-af",
           f"loudnorm=I={lufs:g}:TP=-2:LRA=11", "-ac", "2", "-ar", str(sr),
           "-f", "f32le", "-"]  # fmt: skip
    res = subprocess.run(cmd, capture_output=True, check=True)
    return np.frombuffer(res.stdout, dtype=np.float32).reshape(-1, 2).copy()


def measure_lufs(path: Path) -> float:
    """Integrated loudness (EBU R128) of a media file's audio."""
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-i", str(path), "-vn", "-af", "ebur128",
           "-f", "null", "-"]  # fmt: skip
    err = subprocess.run(cmd, capture_output=True, text=True, check=True).stderr
    summary = err[err.rfind("Integrated loudness:") :]
    for line in summary.splitlines():
        if line.strip().startswith("I:"):
            return float(line.split()[1])
    raise ValueError(f"no loudness reading for {path}")


def playlist(tracks: Sequence[np.ndarray], need: int, crossfade: float, sr: int = SR) -> np.ndarray:
    """The tracks back to back (and round again if needed), crossfaded, at least `need` long."""
    if not tracks:
        raise ValueError("no music tracks")
    x = int(crossfade * sr)
    out = tracks[0]
    k = 1
    while len(out) < need:
        nxt = tracks[k % len(tracks)]
        k += 1
        n = min(x, len(out) // 2, len(nxt) // 2)
        if n:
            ramp = np.linspace(0, 1, n, dtype=np.float32)[:, None]
            mid = out[-n:] * (1 - ramp) + nxt[:n] * ramp
            out = np.concatenate([out[:-n], mid, nxt[n:]])
        else:
            out = np.concatenate([out, nxt])
    return out


def build_bed(
    cues: Sequence[Cue],
    tracks: Sequence[np.ndarray],
    total_seconds: float,
    rules: MusicRules,
    sr: int = SR,
) -> tuple[np.ndarray, list[dict]]:
    """The bed (float32 stereo, `total_seconds` long) and where each cue sits in the playlist."""
    need = sum(max(round(c.duration * sr), 1) for c in cues)
    music = playlist(tracks, need, rules.crossfade_seconds, sr)
    bed = np.zeros((round(total_seconds * sr), 2), dtype=np.float32)
    pos, placed = 0, []
    for c in cues:
        env = envelope(c, sr)
        a = round(c.start * sr)
        n = min(len(env), len(bed) - a)
        if n <= 0:
            continue
        bed[a : a + n] += music[pos : pos + n] * env[:n, None]
        placed.append({"start": c.start, "end": c.end, "music_from": round(pos / sr, 3)})
        pos += n
    return bed, placed


def write_wav(path: Path, x: np.ndarray, sr: int = SR) -> Path:
    """24-bit PCM stereo WAV."""
    import wave

    pcm = np.clip(x, -1.0, 1.0 - 2**-23)
    ints = (pcm * 2**23).astype("<i4")
    raw = ints.astype("<u4").view(np.uint8).reshape(-1, 4)[:, :3].tobytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(3)
        w.setframerate(sr)
        w.writeframes(raw)
    return path


def credits(tracks: Sequence[MusicTrack]) -> str:
    lines = ["# Music", ""]
    for t in tracks:
        lines += [
            f"- **{t.title}** by {t.artist}",
            f"  - source: {t.source_url}",
            f"  - license: {t.license}" + (f" ({t.license_url})" if t.license_url else ""),
            f"  - file: {t.file}",
        ]
    lines += ["", "Attribution is not required by these licenses; this list is the record of "
              "where each track came from."]  # fmt: skip
    return "\n".join(lines) + "\n"


def bed_for_plan(
    plan: EditPlan,
    rules: MusicRules,
    tracks: Sequence[tuple[MusicTrack, Path]],
    out_dir: Path,
    include_title: bool,
    fps: int = 30,
    name: str = "music_bed",
    decoder: Decoder | None = None,
) -> dict:
    """Write `<name>.wav` and `<name>.json` (cues and track record) for one edit."""
    dec = decoder or (lambda p, lufs: decode(p, lufs))
    pieces = layout(plan, fps, include_title)
    cues = music_cues(pieces, rules)
    total = pieces[-1].end if pieces else 0.0
    audio = [dec(path, rules.track_lufs) for _, path in tracks]
    bed, placed = build_bed(cues, audio, total, rules)
    wav = write_wav(out_dir / f"{name}.wav", bed)
    info = {
        "video_id": plan.video_id,
        "include_title": include_title,
        "seconds": round(total, 3),
        "cues": [c.model_dump() for c in cues],
        "placed": placed,
        "music_seconds": round(sum(c.duration for c in cues), 2),
        "track_lufs": rules.track_lufs,
        "bed_db": rules.bed_db,
        "duck_db": rules.duck_db,
        "tracks": [t.model_dump() for t, _ in tracks],
    }
    (out_dir / f"{name}.json").write_text(json.dumps(info, indent=1))
    (out_dir / "music_credits.md").write_text(credits([t for t, _ in tracks]))
    return {"wav": str(wav), **{k: info[k] for k in ("seconds", "music_seconds")},
            "cues": len(cues)}  # fmt: skip
