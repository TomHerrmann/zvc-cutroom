"""Eval runner.

An eval set is a folder:

    evals/sets/<name>/
        manifest.yaml        # name, description, transcript path, per-video spoilers
        transcript.json      # Transcript the cases are cut from
        cases/*.yaml         # one hand-labelled clip per file: label good|bad + the clip

Milestone 1 measures the guardrails: does the validator gate pass every clip a
human marked good and reject every clip a human marked bad, for the right reason?
Milestone 2 adds ranking metrics (does the ranker put human-picked moments on top).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .config import BrandRules
from .schemas import ClipProposal, Transcript, ValidationReport
from .validators import validate_clip


class EvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    label: Literal["good", "bad"]
    expected_codes: list[str] = Field(
        default_factory=list, description="For bad cases: codes the gate must raise."
    )
    notes: str = ""
    clip: ClipProposal


class EvalManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    description: str = ""
    synthetic: bool = Field(
        default=False, description="True if the transcript is not a real ZVC recording."
    )
    transcript: str
    spoilers: list[str] = Field(default_factory=list)


class CaseResult(BaseModel):
    id: str
    label: str
    predicted: Literal["pass", "reject"]
    correct: bool
    expected_codes: list[str]
    got_codes: list[str]
    reason_correct: bool | None
    report: ValidationReport


class EvalResult(BaseModel):
    set_name: str
    synthetic: bool
    run_at: datetime
    git_sha: str | None
    config_hash: str | None
    n_cases: int
    n_good: int
    n_bad: int
    accuracy: float
    good_pass_rate: float = Field(description="Good clips the gate let through.")
    bad_catch_rate: float = Field(description="Bad clips the gate rejected.")
    reason_accuracy: float | None = Field(
        description="Of bad clips with expected codes, share where all expected codes fired."
    )
    cases: list[CaseResult]


def load_set(path: Path) -> tuple[EvalManifest, Transcript, list[EvalCase]]:
    manifest = EvalManifest.model_validate(yaml.safe_load((path / "manifest.yaml").read_text()))
    transcript = Transcript.model_validate_json((path / manifest.transcript).read_text())
    cases = [
        EvalCase.model_validate(yaml.safe_load(p.read_text()))
        for p in sorted((path / "cases").glob("*.yaml"))
    ]
    ids = [c.id for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError(f"duplicate case ids in {path}")
    return manifest, transcript, cases


def _rate(num: int, den: int) -> float:
    return round(num / den, 4) if den else 0.0


def run_eval(path: Path, rules: BrandRules, git_sha: str | None = None) -> EvalResult:
    manifest, transcript, cases = load_set(path)
    results: list[CaseResult] = []
    for case in cases:
        rep = validate_clip(case.clip, transcript, rules, manifest.spoilers)
        predicted = "pass" if rep.passed else "reject"
        correct = (predicted == "pass") == (case.label == "good")
        reason_ok = (
            set(case.expected_codes) <= set(rep.error_codes)
            if case.label == "bad" and case.expected_codes
            else None
        )
        results.append(
            CaseResult(
                id=case.id,
                label=case.label,
                predicted=predicted,
                correct=correct,
                expected_codes=case.expected_codes,
                got_codes=rep.error_codes,
                reason_correct=reason_ok,
                report=rep,
            )
        )
    good = [r for r in results if r.label == "good"]
    bad = [r for r in results if r.label == "bad"]
    with_reason = [r for r in bad if r.reason_correct is not None]
    return EvalResult(
        set_name=manifest.name,
        synthetic=manifest.synthetic,
        run_at=datetime.now(UTC),
        git_sha=git_sha,
        config_hash=rules.config_hash,
        n_cases=len(results),
        n_good=len(good),
        n_bad=len(bad),
        accuracy=_rate(sum(r.correct for r in results), len(results)),
        good_pass_rate=_rate(sum(r.predicted == "pass" for r in good), len(good)),
        bad_catch_rate=_rate(sum(r.predicted == "reject" for r in bad), len(bad)),
        reason_accuracy=(
            _rate(sum(bool(r.reason_correct) for r in with_reason), len(with_reason))
            if with_reason
            else None
        ),
        cases=results,
    )


def to_markdown(res: EvalResult) -> str:
    tag = " (SYNTHETIC fixture, not real ZVC data)" if res.synthetic else ""
    lines = [
        f"# Eval: {res.set_name}{tag}",
        "",
        f"Run {res.run_at:%Y-%m-%d %H:%M} UTC, commit `{res.git_sha}`, "
        f"brand rules `{res.config_hash}`.",
        "",
        "| metric | value |",
        "|---|---|",
        f"| cases | {res.n_cases} ({res.n_good} good, {res.n_bad} bad) |",
        f"| gate accuracy | {res.accuracy:.0%} |",
        f"| good clips passed | {res.good_pass_rate:.0%} |",
        f"| bad clips caught | {res.bad_catch_rate:.0%} |",
        f"| caught for the right reason | "
        f"{'n/a' if res.reason_accuracy is None else f'{res.reason_accuracy:.0%}'} |",
        "",
        "| case | label | gate | ok | codes |",
        "|---|---|---|---|---|",
    ]
    for c in res.cases:
        lines.append(
            f"| {c.id} | {c.label} | {c.predicted} | {'yes' if c.correct else '**no**'}"
            f" | {', '.join(c.got_codes) or '-'} |"
        )
    return "\n".join(lines) + "\n"


def save_result(res: EvalResult, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = res.run_at.strftime("%Y%m%dT%H%M%SZ")
    p = out_dir / f"{stamp}-{res.git_sha or 'nogit'}.json"
    p.write_text(json.dumps(res.model_dump(mode="json"), indent=2))
    (out_dir / "latest.md").write_text(to_markdown(res))
    return p
