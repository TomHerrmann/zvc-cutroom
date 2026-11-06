"""From the clip store to files Resolve can assemble.

`export_plan` writes, per clip: the SRT (plain record of what is said), the ASS caption file
(word highlight), and one `plan.json` entry with the source segments and end card. The
caption overlay is rendered by ffmpeg/libass to QuickTime Animation, because Resolve
composites its alpha (it ignored ProRes 4444 alpha in testing). `resolve_build` then
builds the timelines from `plan.json`.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Iterable
from pathlib import Path

from .captions import write_ass
from .config import BrandRules
from .schemas import ClipProposal, ClipStatus, Format, Transcript
from .subtitles import clip_transcript, write_srt

FPS = 30  # vertical delivery frame rate

# Clips a person or the validators already turned down are never exported.
NOT_EXPORTED = {ClipStatus.REJECTED_BY_HUMAN, ClipStatus.REJECTED_BY_VALIDATOR}


def export_plan(
    clips: Iterable[ClipProposal], transcript: Transcript, rules: BrandRules, out_dir: Path
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    plan = []
    for clip in clips:
        if clip.status in NOT_EXPORTED or clip.format != Format.VERTICAL:
            continue
        ct = clip_transcript(transcript, clip)
        write_srt(ct, out_dir / f"{clip.id}.srt")
        card = rules.end_card if clip.end_card else None
        # captions stop where the end card starts to slam over the video
        card_in = clip.duration - (card.overlap_frames / FPS if card else 0)
        write_ass(ct, rules, out_dir / f"{clip.id}.ass", end_at=card_in)
        plan.append(
            {
                "id": clip.id,
                "video_id": clip.video_id,
                "segments": [[round(g.start, 3), round(g.end, 3)] for g in clip.segments],
                "duration": round(clip.duration, 3),
                "srt": f"{clip.id}.srt",
                "captions": f"{clip.id}.ass",
                "overlay": f"{clip.id}.captions.mov",
                "end_card": card
                and {
                    "title": card.title,
                    "topic_title": clip.end_card,
                    "label": card.label,
                    "seconds": card.seconds,
                    "transition_in": card.transition_in,
                    "overlap_frames": card.overlap_frames,
                },
            }
        )
    p = out_dir / "plan.json"
    p.write_text(json.dumps(plan, indent=1))
    return p


def overlay_command(ass: Path, duration: float, out: Path, fonts_dir: Path | None) -> list[str]:
    """ffmpeg call that renders an ASS file to a transparent 1080x1920 overlay."""
    filt = f"ass={ass.name}" + (f":fontsdir={fonts_dir}" if fonts_dir else "") + ":alpha=1"
    return [
        "ffmpeg", "-nostdin", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"color=c=black@0.0:s=1080x1920:r={FPS}:d={duration},format=rgba",
        "-vf", f"{filt},format=argb", "-c:v", "qtrle", out.name,
    ]  # fmt: skip


def render_overlays(
    plan_path: Path,
    fonts_dir: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> list[Path]:
    """Render every clip's caption overlay next to plan.json. Returns the files written."""
    base = plan_path.parent
    out = []
    for p in json.loads(plan_path.read_text()):
        target = base / p["overlay"]
        cmd = overlay_command(base / p["captions"], p["duration"], target, fonts_dir)
        res = runner(cmd, cwd=base, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"ffmpeg failed for {p['id']}: {res.stderr[-400:]}")
        out.append(target)
    return out
