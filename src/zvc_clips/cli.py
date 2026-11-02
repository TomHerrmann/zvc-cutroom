"""`zvc` command line. CLI first, UI second."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from statistics import median

import typer
import yaml
from rich.console import Console
from rich.table import Table

from . import ingest
from .config import load_rules
from .evals import run_eval, save_result
from .runlog import git_sha, read_runs, run
from .schemas import ClipProposal, Transcript
from .store import ClipStore
from .validators import validate_clip

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Zero Vision Cinema long video -> validated short clips.",
)
console = Console()

RulesOpt = typer.Option(None, "--rules", help="Brand rules YAML (default: config/brand_rules.yaml)")


@app.command()
def transcribe(
    video: Path = typer.Argument(..., exists=True, help="Source video file."),
    video_id: str = typer.Option(
        ..., "--id", help="Stable id for this video, e.g. cronov-2026-e01"
    ),
    out_dir: Path = typer.Option(Path("data"), help="Where audio + transcript are written."),
    model: str = typer.Option("large-v3", help="faster-whisper model size."),
) -> None:
    """Extract audio with ffmpeg and transcribe with Whisper (word timestamps)."""
    with run("transcribe") as r:
        audio = ingest.extract_audio(video, out_dir / video_id / "audio.wav")
        t = ingest.transcribe_whisper(audio, video_id, model_size=model)
        p = out_dir / video_id / "transcript.whisper.json"
        p.write_text(t.model_dump_json(indent=2))
        r.record.n_items = len(t.segments)
        r.record.model = t.model
        r.record.extra = {"duration_s": t.duration, "words": len(t.words())}
    console.print(f"[green]wrote[/] {p}  ({len(t.segments)} segments, {t.duration:.0f}s)")


@app.command("import-srt")
def import_srt(
    srt: Path = typer.Argument(..., exists=True, help="Subtitles exported from Resolve."),
    video_id: str = typer.Option(..., "--id"),
    duration: float = typer.Option(..., help="Video duration in seconds."),
    out_dir: Path = typer.Option(Path("data")),
) -> None:
    """Import Resolve's transcription (exported as SRT) for cross-checking."""
    t = ingest.load_srt(srt, video_id, duration)
    p = out_dir / video_id / "transcript.resolve.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(t.model_dump_json(indent=2))
    console.print(f"[green]wrote[/] {p}  ({len(t.segments)} cues)")


@app.command()
def crosscheck(
    reference: Path = typer.Argument(..., exists=True, help="Whisper transcript JSON."),
    other: Path = typer.Argument(..., exists=True, help="Resolve transcript JSON."),
    show: int = typer.Option(15, help="How many disagreements to print."),
) -> None:
    """Compare two transcripts: WER plus the regions where they disagree."""
    a = Transcript.model_validate_json(reference.read_text())
    b = Transcript.model_validate_json(other.read_text())
    with run("crosscheck") as r:
        res = ingest.crosscheck(a, b)
        r.record.extra = {"wer": res.wer, "disagreements": len(res.disagreements)}
    console.print(
        f"WER {res.wer:.1%} over {res.reference_words} words, "
        f"{len(res.disagreements)} disagreement regions"
    )
    for d in res.disagreements[:show]:
        console.print(f"  {d.start:7.1f}s  whisper: {d.reference!r}  resolve: {d.other!r}")


@app.command()
def validate(
    clips: Path = typer.Argument(..., exists=True, help="Clip JSON/YAML file or a folder of them."),
    transcript: Path = typer.Option(..., "--transcript", "-t", exists=True),
    rules_path: Path | None = RulesOpt,
    spoiler: list[str] = typer.Option([], help="Extra per-video spoiler terms."),
) -> None:
    """Run every validator on one or more clip proposals."""
    rules = load_rules(rules_path)
    t = Transcript.model_validate_json(transcript.read_text())
    files = (
        sorted(clips.glob("*.json")) + sorted(clips.glob("*.y*ml")) if clips.is_dir() else [clips]
    )
    with run("validate", rules.config_hash) as r:
        reports = []
        for f in files:
            data = (
                json.loads(f.read_text()) if f.suffix == ".json" else yaml.safe_load(f.read_text())
            )
            clip = ClipProposal.model_validate(data.get("clip", data))
            reports.append(validate_clip(clip, t, rules, spoiler))
        r.add_reports(reports)
    for rep in reports:
        mark = "[green]PASS[/]" if rep.passed else "[red]REJECT[/]"
        console.print(f"{mark} {rep.clip_id}")
        for i in rep.issues:
            console.print(f"    {i.severity.value:7} {i.code:26} {i.message}")
    if not all(rep.passed for rep in reports):
        raise typer.Exit(1)


@app.command("eval")
def eval_cmd(
    set_path: Path = typer.Argument(..., exists=True, file_okay=False),
    rules_path: Path | None = RulesOpt,
    out_dir: Path = typer.Option(Path("evals/results")),
) -> None:
    """Run an eval set and save results (JSON + latest.md)."""
    rules = load_rules(rules_path)
    with run("eval", rules.config_hash) as r:
        res = run_eval(set_path, rules, git_sha=git_sha())
        r.add_reports([c.report for c in res.cases])
        r.record.extra = {
            "set": res.set_name,
            "accuracy": res.accuracy,
            "bad_catch_rate": res.bad_catch_rate,
            "good_pass_rate": res.good_pass_rate,
        }
    p = save_result(res, out_dir / res.set_name)
    tag = " [yellow](synthetic)[/]" if res.synthetic else ""
    console.print(
        f"{res.set_name}{tag}: accuracy {res.accuracy:.0%}, good passed "
        f"{res.good_pass_rate:.0%}, bad caught {res.bad_catch_rate:.0%} -> {p}"
    )
    for c in res.cases:
        if not c.correct:
            console.print(f"  [red]wrong[/] {c.id} ({c.label}) -> {c.predicted} {c.got_codes}")
    if res.accuracy < 1.0:
        raise typer.Exit(1)


@app.command()
def runs(last: int = typer.Option(20, help="How many recent runs to show.")) -> None:
    """Summarise the run log: latency, cost, pass/fail rates."""
    records = read_runs()
    if not records:
        console.print("no runs logged yet")
        return
    table = Table("when", "command", "ok", "ms", "cost $", "items", "pass", "fail", "sha")
    for rec in records[-last:]:
        table.add_row(
            f"{rec.started_at:%m-%d %H:%M}",
            rec.command,
            "y" if rec.ok else "n",
            f"{rec.latency_ms:.0f}",
            f"{rec.cost_usd:.4f}",
            str(rec.n_items),
            str(rec.n_passed),
            str(rec.n_failed),
            rec.git_sha or "-",
        )
    console.print(table)
    items = sum(r.n_items for r in records)
    codes: Counter[str] = Counter()
    for r in records:
        codes.update(r.failure_codes)
    console.print(
        f"{len(records)} runs, median latency "
        f"{median(r.latency_ms for r in records):.0f} ms, total cost "
        f"${sum(r.cost_usd for r in records):.4f}, pass rate "
        f"{sum(r.n_passed for r in records) / max(items, 1):.0%} over {items} items"
    )
    if codes:
        console.print(
            "top failure codes: " + ", ".join(f"{c} x{n}" for c, n in codes.most_common(5))
        )


@app.command("queue")
def queue(
    video_id: str | None = typer.Option(None, "--id"), root: Path = typer.Option(Path("clips"))
) -> None:
    """List clips in the store with their review status."""
    for c in ClipStore(root).list(video_id):
        console.print(
            f"{c.status.value:22} {c.video_id}/{c.id}  {c.start:.1f}-{c.end:.1f}s  "
            f"{c.platform.value}  {c.hook}"
        )


if __name__ == "__main__":
    app()
