"""Run logging. Every command appends one RunRecord line to runs/runs.jsonl."""

from __future__ import annotations

import subprocess
import time
import uuid
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .schemas import RunRecord, ValidationReport

DEFAULT_LOG = Path("runs/runs.jsonl")

# USD per million tokens. Keep in sync with the provider's price page; the run log
# stores the computed cost so old runs keep the price they were billed at.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {}


def git_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        )
        return out.stdout.strip() or None
    except (OSError, subprocess.CalledProcessError):
        return None


class Run:
    def __init__(self, record: RunRecord):
        self.record = record

    def add_usage(self, model: str, input_tokens: int, output_tokens: int) -> None:
        r = self.record
        r.model = model
        r.input_tokens += input_tokens
        r.output_tokens += output_tokens
        if model in PRICES_PER_MTOK:
            pin, pout = PRICES_PER_MTOK[model]
            r.cost_usd += (input_tokens * pin + output_tokens * pout) / 1_000_000

    def add_reports(self, reports: list[ValidationReport]) -> None:
        r = self.record
        r.n_items += len(reports)
        r.n_passed += sum(rep.passed for rep in reports)
        r.n_failed += sum(not rep.passed for rep in reports)
        counts = Counter(r.failure_codes)
        for rep in reports:
            counts.update(rep.error_codes)
        r.failure_codes = dict(counts)


@contextmanager
def run(
    command: str, config_hash: str | None = None, log_path: Path = DEFAULT_LOG
) -> Iterator[Run]:
    rec = RunRecord(
        run_id=uuid.uuid4().hex[:10], command=command, git_sha=git_sha(), config_hash=config_hash
    )
    handle = Run(rec)
    t0 = time.perf_counter()
    try:
        yield handle
    except Exception as e:
        rec.ok = False
        rec.error = f"{type(e).__name__}: {e}"
        raise
    finally:
        rec.latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a") as f:
            f.write(rec.model_dump_json() + "\n")


def read_runs(log_path: Path = DEFAULT_LOG) -> list[RunRecord]:
    if not log_path.exists():
        return []
    return [
        RunRecord.model_validate_json(line)
        for line in log_path.read_text().splitlines()
        if line.strip()
    ]
