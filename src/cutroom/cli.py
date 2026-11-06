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
from .schemas import ClipProposal, ClipStatus, Transcript, VideoContext
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


@app.command("align")
def align_cmd(
    transcript: Path = typer.Argument(..., exists=True, help="transcript.reviewed.json"),
    audio: Path = typer.Option(..., exists=True, help="16 kHz mono WAV of the source video."),
) -> None:
    """Time every reviewed word against the audio (forced alignment, no transcription).

    Writes transcript.aligned.json next to the input. Needs the `align` extra.
    """
    from .align import align_transcript
    from .audio import Level

    t = Transcript.model_validate_json(transcript.read_text())
    silences = Level.from_wav(audio).silences(0, t.duration + 1, 0.04)
    with run("align") as r:
        aligned, failed = align_transcript(t, audio, silences)
        r.record.extra["lines_unaligned"] = failed
    out = transcript.with_name("transcript.aligned.json")
    out.write_text(aligned.model_dump_json(indent=1))
    console.print(f"[green]wrote[/] {out}  ({len(aligned.words())} words)")
    if failed:
        console.print(f"[yellow]{len(failed)} lines kept estimated times:[/] {failed}")


@app.command("video-context")
def video_context_cmd(
    video: Path = typer.Argument(..., exists=True, help="The finished long-form video."),
    video_id: str = typer.Option(..., "--id"),
    youtube_only: list[str] = typer.Option(
        [], help="Long-form-only range in seconds, e.g. 8.23-14.53 for the title card."
    ),
    long_review: str | None = typer.Option(None, help="Title or URL of the full YouTube review."),
    scenes: bool = typer.Option(False, help="Print hard cuts in the first minute (title card)."),
    out_dir: Path = typer.Option(Path("data")),
) -> None:
    """Audio + silence map + per-video facts -> data/ID/audio16k.wav and video.json."""
    from .video import build_context, extract_audio, parse_range, scene_changes

    d = out_dir / video_id
    wav = extract_audio(video, d / "audio16k.wav")
    if scenes:
        console.print(f"hard cuts in the first minute: {scene_changes(video)}")
    ctx = build_context(video_id, wav, [parse_range(r) for r in youtube_only], long_review)
    (d / "video.json").write_text(ctx.model_dump_json(indent=1))
    console.print(f"[green]wrote[/] {d / 'video.json'}  ({len(ctx.silences)} silences)")


@app.command("export")
def export_cmd(
    video_id: str = typer.Option(..., "--id"),
    transcript: Path = typer.Option(..., "--transcript", "-t", exists=True),
    out_dir: Path = typer.Option(..., "--out", help="Folder Resolve can read."),
    root: Path = typer.Option(Path("clips")),
    rules_path: Path | None = RulesOpt,
) -> None:
    """Clip store -> plan.json + per-clip SRT and caption (ASS) files."""
    from .render import export_plan

    rules = load_rules(rules_path)
    t = Transcript.model_validate_json(transcript.read_text())
    p = export_plan(ClipStore(root).list(video_id), t, rules, out_dir)
    console.print(f"[green]wrote[/] {p}  ({len(json.loads(p.read_text()))} clips)")


@app.command("captions")
def captions_cmd(
    plan: Path = typer.Argument(..., exists=True, help="plan.json from `cutroom export`."),
    fonts: Path | None = typer.Option(None, help="Folder with the caption font, relative to plan."),
) -> None:
    """Render each clip's word-highlight caption overlay (needs ffmpeg with libass)."""
    from .render import render_overlays

    for f in render_overlays(plan, fonts):
        console.print(f"[green]wrote[/] {f}")


@app.command("resolve-build")
def resolve_build_cmd(
    plan: Path = typer.Argument(..., exists=True),
    source: Path = typer.Option(..., exists=True, help="The long-form source video."),
    project: str | None = typer.Option(None, help="Resolve project (default: the open one)."),
    reframe: bool = typer.Option(True, help="Run Smart Reframe on every cut."),
) -> None:
    """Build every clip's timeline in DaVinci Resolve Studio, then check it against the plan."""
    from . import resolve_build

    try:
        import DaVinciResolveScript as dvr  # type: ignore[import-not-found]
    except ImportError as e:
        console.print("[red]Resolve scripting API not found.[/] Run this on the editing machine "
                      "with Resolve open and its Modules folder on PYTHONPATH.")  # fmt: skip
        raise typer.Exit(2) from e
    resolve = dvr.scriptapp("Resolve")
    with run("resolve-build") as r:
        done = resolve_build.build_all(resolve, plan, source, project, reframe=reframe)
        proj = resolve.GetProjectManager().GetCurrentProject()
        problems = [p for e in json.loads(plan.read_text())
                    for p in resolve_build.check_clip(proj, e)]  # fmt: skip
        r.record.extra = {"built": done, "problems": problems}
    for d in done:
        console.print(
            f"[green]built[/] {d['id']}: {d['segments']} cuts, {d['video_frames']} frames"
        )
    for p in problems:
        console.print(f"[red]check failed[/] {p}")
    if problems:
        raise typer.Exit(1)


@app.command("propose")
def propose_cmd(
    transcript: Path = typer.Argument(..., exists=True, help="transcript.aligned.json"),
    video: Path | None = typer.Option(
        None, exists=True, help="video.json: silences, YouTube-only ranges, long review."
    ),
    audio: Path | None = typer.Option(
        None, exists=True, help="16 kHz mono WAV: snap cuts with the audio level map."
    ),
    source: Path | None = typer.Option(
        None, exists=True, help="Source video: face checks for covers and face_on_screen."
    ),
    draft_file: Path | None = typer.Option(
        None, exists=True, help="Use a saved draft instead of calling Claude (no repairs)."
    ),
    spoiler: list[str] = typer.Option([], help="Extra per-video spoiler terms."),
    about: str | None = typer.Option(
        None, help="One or two sentences on the video's format, e.g. 'a vlog, not a review'."
    ),
    root: Path = typer.Option(Path("clips")),
    rules_path: Path | None = RulesOpt,
) -> None:
    """Claude scores moments and drafts clips; validators gate them; passing clips are queued.

    Writes proposal_draft.json next to the transcript (re-run it with --draft-file) and one
    clip per moment to the store: pending_review if it passed, rejected_by_validator if not.
    """
    from . import propose as P

    rules = load_rules(rules_path)
    t = Transcript.model_validate_json(transcript.read_text())
    ctx = VideoContext.model_validate_json(video.read_text()) if video else None
    level = None
    if audio:
        from .audio import Level

        level = Level.from_wav(audio)
    faces = None
    if source:
        try:
            from .faces import FaceProbe
        except ImportError as e:  # pragma: no cover
            raise typer.BadParameter("--source needs the `faces` extra") from e
        faces = FaceProbe(source)
    store = ClipStore(root)
    with run("propose", rules.config_hash) as r:
        if draft_file:
            draft = P.ProposalDraft.model_validate_json(draft_file.read_text())
            repair = None
        else:
            draft = P.draft_with_claude(rules, t, ctx, spoiler, level, about, run=r)
            P.save_draft(draft, transcript.with_name("proposal_draft.json"))

            def repair(failed):
                return P.repair_with_claude(rules, t, failed, ctx, spoiler, level, about, run=r)

        results = P.propose(rules, t, draft, ctx, level, faces, spoiler, repair)
        # Replace this video's earlier machine proposals, but never one a person has judged.
        judged = {ClipStatus.APPROVED, ClipStatus.REJECTED_BY_HUMAN, ClipStatus.POSTED}
        existing = {c.id: c for c in store.list(t.video_id)}
        for c in existing.values():
            if c.created_by == "llm" and c.status not in judged:
                store.remove(c.video_id, c.id)
        for res in results:
            if res.clip is None:
                continue
            old = existing.get(res.clip.id)
            if old is not None and (old.created_by != "llm" or old.status in judged):
                res.clip.id = f"{res.clip.id}-new"
            store.put(res.clip)
        r.add_reports([res.report for res in results if res.report is not None])
        r.record.n_items = len(results)
        r.record.n_failed = sum(not res.passed for res in results)
        r.record.n_passed = r.record.n_items - r.record.n_failed
        passed = [res.clip for res in results if res.passed and res.clip]
        human = [c for c in existing.values() if c.created_by == "human"]
        matches, recall = P.match_picks(passed, human) if human else ([], None)
        r.record.extra = {
            "repaired": sum(res.attempts > 1 and res.passed for res in results),
            "uncompiled": sum(res.clip is None for res in results),
            "human_pick_recall": recall,
            "from_draft_file": bool(draft_file),
        }
    for res in results:
        mark = "[green]PASS[/]" if res.passed else "[red]REJECT[/]"
        cid = res.clip.id if res.clip else res.slug
        dur = f"{res.clip.duration:5.1f}s" if res.clip else "   -  "
        tries = f" (after {res.attempts - 1} repair)" if res.attempts > 1 else ""
        console.print(f"{mark} {res.rank:.2f}  {cid:28} {dur}  {res.draft.hook}{tries}")
        for i in res.problems():
            console.print(f"      {i.code:26} {i.message}")
    for m in matches:
        where = f"{m.auto} (#{m.auto_rank}, overlap {m.overlap:.2f})" if m.auto else "no match"
        console.print(f"  your pick {m.human:16} -> {where}")
    if recall is not None:
        console.print(f"human picks covered: {recall:.0%}")
    console.print(
        f"{r.record.n_passed}/{r.record.n_items} passed, ${r.record.cost_usd:.4f}, "
        f"{r.record.latency_ms / 1000:.1f}s"
    )
    if faces:
        faces.close()


@app.command("review")
def review_cmd(
    video_id: str = typer.Argument(...),
    clip_id: str = typer.Argument(...),
    verdict: str = typer.Argument(..., help="post | never"),
    note: str | None = typer.Option(None, help="Why. Feeds the real eval set."),
    root: Path = typer.Option(Path("clips")),
) -> None:
    """Record a person's verdict on a clip: would post it, or never would."""
    status = {"post": ClipStatus.APPROVED, "never": ClipStatus.REJECTED_BY_HUMAN}.get(verdict)
    if status is None:
        raise typer.BadParameter("verdict must be 'post' or 'never'")
    store = ClipStore(root)
    clip = store.get(video_id, clip_id).model_copy(update={"status": status, "review_note": note})
    store.put(clip)
    console.print(f"{clip.id}: {status.value}" + (f"  ({note})" if note else ""))


@app.command()
def validate(
    clips: Path = typer.Argument(..., exists=True, help="Clip JSON/YAML file or a folder of them."),
    transcript: Path = typer.Option(..., "--transcript", "-t", exists=True),
    rules_path: Path | None = RulesOpt,
    spoiler: list[str] = typer.Option([], help="Extra per-video spoiler terms."),
    video: Path | None = typer.Option(
        None,
        exists=True,
        help="video.json (silences, YouTube-only ranges, long review). Without it the "
        "audio-based cut checks and the end-card rules are skipped.",
    ),
) -> None:
    """Run every validator on one or more clip proposals."""
    rules = load_rules(rules_path)
    t = Transcript.model_validate_json(transcript.read_text())
    ctx = VideoContext.model_validate_json(video.read_text()) if video else None
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
            reports.append(validate_clip(clip, t, rules, spoiler, video=ctx))
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
            f"{c.status.value:22} {c.video_id}/{c.id}  {len(c.segments)} seg {c.duration:.1f}s  "
            f"{c.format.value}  {c.hook}"
        )


if __name__ == "__main__":
    app()
