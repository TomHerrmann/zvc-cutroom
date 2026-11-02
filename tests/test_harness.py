from conftest import SMOKE_SET
from typer.testing import CliRunner

from cutroom import runlog
from cutroom.cli import app
from cutroom.evals import run_eval
from cutroom.ingest import crosscheck, load_srt
from cutroom.schemas import ClipStatus, Segment, Transcript, ValidationReport, Word
from cutroom.store import ClipStore


def test_smoke_eval_set_is_fully_correct(rules):
    res = run_eval(SMOKE_SET, rules)
    assert res.synthetic
    wrong = [(c.id, c.got_codes) for c in res.cases if not c.correct or c.reason_correct is False]
    assert not wrong
    assert res.n_good >= 3 and res.n_bad >= 10


def test_crosscheck_counts_errors(transcript):
    other = transcript.model_copy(deep=True)
    seg = other.segments[2]
    seg.words[0] = Word(text="There's", start=seg.words[0].start, end=seg.words[0].end)
    res = crosscheck(transcript, other, min_words=1)
    assert res.wer > 0
    assert res.disagreements[0].reference == "here's"
    assert crosscheck(transcript, transcript).wer == 0


def test_load_srt(tmp_path):
    p = tmp_path / "r.srt"
    p.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nHello there\n\n"
        "2\n00:00:02,500 --> 00:00:04,000\nGeneral\nKenobi\n"
    )
    t = load_srt(p, "v", duration=10)
    assert [s.text for s in t.segments] == ["Hello there", "General Kenobi"]
    assert t.words()[1].end == 2.0


def test_store_roundtrip(tmp_path, good_clip):
    store = ClipStore(tmp_path)
    store.put(good_clip)
    assert store.get("synthetic-001", "t1") == good_clip
    store.set_status("synthetic-001", "t1", ClipStatus.APPROVED)
    assert [c.id for c in store.list(status=ClipStatus.APPROVED)] == ["t1"]


def test_runlog_records_failures(tmp_path):
    log = tmp_path / "runs.jsonl"
    with runlog.run("test", log_path=log) as r:
        r.add_reports(
            [
                ValidationReport(clip_id="a"),
                ValidationReport.model_validate(
                    {"clip_id": "b", "issues": [{"code": "SPOILER", "message": "m"}]}
                ),
            ]
        )
    try:
        with runlog.run("boom", log_path=log):
            raise RuntimeError("x")
    except RuntimeError:
        pass
    recs = runlog.read_runs(log)
    assert (recs[0].n_passed, recs[0].n_failed, recs[0].failure_codes) == (1, 1, {"SPOILER": 1})
    assert recs[1].ok is False and "RuntimeError" in recs[1].error


def test_cli_eval_and_validate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    res = runner.invoke(app, ["eval", str(SMOKE_SET), "--out-dir", str(tmp_path / "out")])
    assert res.exit_code == 0, res.output
    assert (tmp_path / "out" / "synthetic_smoke" / "latest.md").exists()
    bad = SMOKE_SET / "cases" / "bad-spoiler.yaml"
    res = runner.invoke(
        app,
        [
            "validate",
            str(bad),
            "-t",
            str(SMOKE_SET / "transcript.json"),
            "--spoiler",
            "narrator was dead",
        ],
    )
    assert res.exit_code == 1 and "SPOILER" in res.output
    assert len(runlog.read_runs(tmp_path / "runs" / "runs.jsonl")) == 2


def test_transcript_text_between():
    t = Transcript(
        video_id="v",
        source="manual",
        duration=5,
        segments=[
            Segment(
                id=0,
                start=0,
                end=2,
                text="a b",
                words=[Word(text="a", start=0, end=1), Word(text="b", start=1, end=2)],
            )
        ],
    )
    assert t.text_between(0.5, 2) == "b"
