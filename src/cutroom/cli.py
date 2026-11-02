"""`cutroom` command line. CLI first, UI second."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from statistics import median

import typer
import yaml
from rich.console import Console
from rich.table import Table

from . import subtitles
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


@app.command("import-srt")
def import_srt(
    srt: Path = typer.Argument(..., exists=True, help="Subtitle track exported from Resolve."),
    video_id: str = typer.Option(..., "--id", help="Stable id, e.g. cronov-2026-e01"),
    duration: float = typer.Option(..., help="Video duration in seconds."),
    out_dir: Path = typer.Option(Path("data")),
) -> None:
    """Import Resolve's AI subtitles (exported as SRT) as the working transcript."""
    t = subtitles.load_srt(srt, video_id, duration)
    p = out_dir / video_id / "transcript.resolve.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(t.model_dump_json(indent=2))
    console.print(f"[green]wrote[/] {p}  ({len(t.segments)} cues)")


@app.command("review-subs")
def review_subs(
    transcript: Path = typer.Argument(..., exists=True, help="transcript.resolve.json"),
    glossary: Path | None = typer.Option(
        None, exists=True, help="Text file, one name or film title per line."
    ),
    review_file: Path | None = typer.Option(
        None, exists=True, help="Apply a saved review instead of calling Claude."
    ),
    answers: Path | None = typer.Option(
        None, exists=True, help="YAML list of your rulings on unverified words."
    ),
    rules_path: Path | None = RulesOpt,
) -> None:
    """Claude accuracy pass on the subtitles, then write the corrected SRT for Resolve.

    Words Claude can't verify are never changed automatically. They are written to
    subtitle_questions.md with the sentence before, the sentence, and the sentence after,
    and stay as transcribed until you answer them with --answers.
    """
    rules = load_rules(rules_path)
    t = Transcript.model_validate_json(transcript.read_text())
    out = transcript.parent
    terms = (
        [ln.strip() for ln in glossary.read_text().splitlines() if ln.strip()] if glossary else []
    )
    with run("review-subs", rules.config_hash) as r:
        if review_file:
            review = subtitles.SubtitleReview.model_validate_json(review_file.read_text())
        else:
            review = subtitles.review_with_claude(t, rules.subtitles.review_model, terms, run=r)
            (out / "subtitle_review.json").write_text(review.model_dump_json(indent=2))
        fixed, issues = subtitles.apply_review(t, review)
        issues += subtitles.check_unverified(t, review)
        rulings = []
        if answers:
            rulings = [
                subtitles.HumanAnswer.model_validate(a) for a in yaml.safe_load(answers.read_text())
            ]
            fixed, answer_issues = subtitles.apply_answers(fixed, rulings)
            issues += answer_issues
        answered = {(a.cue_id, a.phrase) for a in rulings}
        open_flags = [f for f in review.unverified if (f.cue_id, f.phrase) not in answered]
        (out / "subtitle_questions.md").write_text(
            subtitles.questions_markdown(t, review.model_copy(update={"unverified": open_flags}))
        )
        errors = [i for i in issues if i.severity.value == "error"]
        r.record.n_items = len(review.corrections)
        r.record.n_failed = len({i.field for i in errors})
        r.record.n_passed = r.record.n_items - r.record.n_failed
        r.record.failure_codes = dict(Counter(i.code for i in errors))
        r.record.extra = {
            "unverified": len(review.unverified),
            "open_questions": len(open_flags),
            "human_answers": len(rulings),
        }
        (out / "transcript.reviewed.json").write_text(fixed.model_dump_json(indent=2))
        srt = subtitles.write_srt(fixed, out / "subtitles.reviewed.srt")
    for c in review.corrections:
        console.print(f"  [{c.cue_id}] {c.kind:11} {c.original!r} -> {c.corrected!r}")
    for i in issues:
        console.print(f"  {i.severity.value:7} {i.code:18} {i.message}")
    for f in open_flags:
        console.print(f"  [yellow]unverified[/] [{f.cue_id}] {f.phrase!r} (guess: {f.best_guess})")
    if open_flags:
        console.print(
            f"[yellow]{len(open_flags)} question(s) for you:[/] {out / 'subtitle_questions.md'}"
        )
    console.print(
        f"[green]wrote[/] {srt}  ({r.record.n_passed} corrections applied, "
        f"{r.record.n_failed} rejected, ${r.record.cost_usd:.4f})"
    )


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
