from conftest import SMOKE_SET
from typer.testing import CliRunner

from cutroom import runlog, subtitles
from cutroom.cli import app
from cutroom.evals import run_eval
from cutroom.schemas import ClipStatus, Segment, Transcript, ValidationReport, Word
from cutroom.store import ClipStore
from cutroom.subtitles import SubtitleCorrection, SubtitleReview, load_srt, write_srt


def test_smoke_eval_set_is_fully_correct(rules):
    res = run_eval(SMOKE_SET, rules)
    assert res.synthetic
    wrong = [(c.id, c.got_codes) for c in res.cases if not c.correct or c.reason_correct is False]
    assert not wrong
    assert res.n_good >= 3 and res.n_bad >= 10


SRT = (
    "1\n00:00:01,000 --> 00:00:02,000\nHello there\n\n"
    "2\n00:00:02,500 --> 00:00:04,000\nwe watched blade runner\nby riddley scot\n"
)


def _srt_transcript(tmp_path):
    p = tmp_path / "r.srt"
    p.write_text(SRT)
    return load_srt(p, "v", duration=10)


def test_load_srt(tmp_path):
    t = _srt_transcript(tmp_path)
    assert t.word_timing == "estimated" and t.source == "resolve"
    assert [s.text for s in t.segments] == [
        "Hello there",
        "we watched blade runner by riddley scot",
    ]
    assert t.words()[1].end == 2.0


def test_srt_roundtrip(tmp_path):
    t = _srt_transcript(tmp_path)
    out = write_srt(t, tmp_path / "out.srt").read_text()
    assert "00:00:02,500 --> 00:00:04,000" in out
    t2 = load_srt(tmp_path / "out.srt", "v", duration=10)
    assert [(s.start, s.end, s.text) for s in t2.segments] == [
        (s.start, s.end, s.text) for s in t.segments
    ]


def _fix(cue_id, original, corrected, kind="proper_noun"):
    return SubtitleCorrection(
        cue_id=cue_id, original=original, corrected=corrected, kind=kind, reason="r"
    )


def test_apply_review_keeps_timing_and_rejects_bad_corrections(tmp_path):
    t = _srt_transcript(tmp_path)
    review = SubtitleReview(
        corrections=[
            _fix(
                1,
                "we watched blade runner by riddley scot",
                "we watched Blade Runner by Ridley Scott",
            ),
            _fix(0, "Hi there", "Hello there!"),  # original doesn't match the cue
            _fix(7, "nothing", "something"),  # cue doesn't exist
        ]
    )
    fixed, issues = subtitles.apply_review(t, review)
    assert {i.code for i in issues} == {"ORIGINAL_MISMATCH", "UNKNOWN_CUE"}
    assert fixed.segments[1].text == "we watched Blade Runner by Ridley Scott"
    assert fixed.segments[0].text == "Hello there"  # rejected correction not applied
    assert [(s.start, s.end) for s in fixed.segments] == [(s.start, s.end) for s in t.segments]
    assert fixed.source == "resolve_reviewed"


def test_large_rewrite_is_flagged(tmp_path):
    t = _srt_transcript(tmp_path)
    review = SubtitleReview(corrections=[_fix(0, "Hello there", "General Kenobi, you are bold")])
    _, issues = subtitles.apply_review(t, review)
    assert [i.code for i in issues] == ["LARGE_EDIT"]


class _FakeUsage:
    input_tokens = 1000
    output_tokens = 200


class _FakeResponse:
    model = "claude-opus-5-5"
    usage = _FakeUsage()
    stop_reason = "end_turn"
    parsed_output = SubtitleReview(corrections=[])


class _FakeClient:
    def __init__(self):
        self.calls = []
        outer = self

        class _Messages:
            def parse(self, **kw):
                outer.calls.append(kw)
                return _FakeResponse()

        class _Beta:
            messages = _Messages()

        self.beta = _Beta()


def test_review_with_claude_logs_cost_and_sends_glossary(tmp_path):
    t = _srt_transcript(tmp_path)
    client = _FakeClient()
    with runlog.run("t", log_path=tmp_path / "runs.jsonl") as r:
        subtitles.review_with_claude(t, "claude-opus-5-5", ["Ridley Scott"], run=r, client=client)
    call = client.calls[0]
    assert call["output_format"] is SubtitleReview
    assert "- Ridley Scott" in call["messages"][0]["content"]
    assert "[1] we watched blade runner" in call["messages"][0]["content"]
    assert r.record.cost_usd == (1000 * 4 + 200 * 20) / 1_000_000


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


def test_unverified_words_are_never_auto_applied(tmp_path):
    from cutroom.subtitles import HumanAnswer, UnverifiedPhrase

    t = _srt_transcript(tmp_path)
    review = SubtitleReview(
        corrections=[],
        unverified=[
            UnverifiedPhrase(
                cue_id=1, phrase="riddley scot", best_guess="Ridley Scott", reason="r"
            ),
            UnverifiedPhrase(cue_id=0, phrase="not there", best_guess=None, reason="r"),
        ],
    )
    fixed, issues = subtitles.apply_review(t, review)
    issues += subtitles.check_unverified(t, review)
    assert fixed.segments[1].text == t.segments[1].text  # guess not applied
    assert [i.code for i in issues] == ["FLAG_NOT_IN_CUE"]

    ctx = subtitles.flag_context(t, 1, "riddley scot")
    # no end punctuation between the cues, so they read as one sentence
    assert ctx["sentence"] == "Hello there we watched blade runner by riddley scot"
    assert "riddley scot" in subtitles.questions_markdown(t, review)

    answered, issues = subtitles.apply_answers(
        fixed, [HumanAnswer(cue_id=1, phrase="riddley scot", replacement="Ridley Scott")]
    )
    assert not issues
    assert answered.segments[1].text == "we watched blade runner by Ridley Scott"
    _, issues = subtitles.apply_answers(
        fixed, [HumanAnswer(cue_id=0, phrase="nope", replacement="x")]
    )
    assert [i.code for i in issues] == ["ANSWER_NOT_APPLIED"]
