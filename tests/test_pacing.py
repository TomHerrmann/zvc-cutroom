"""Long-form pacing: talking at real time, work sped up by kind, dead air cut."""

import base64
import json
import shutil
import subprocess
from types import SimpleNamespace

import numpy as np
import pytest
from typer.testing import CliRunner

from cutroom import pacing as PC
from cutroom.cli import app
from cutroom.config import PacingRules
from cutroom.propose import LineRange
from cutroom.runlog import Run
from cutroom.schemas import RunRecord, Segment, Transcript, Word


def line(i, start, text):
    words, t = [], start
    for w in text.split():
        words.append(Word(text=w, start=round(t, 3), end=round(t + 0.3, 3)))
        t += 0.35
    return Segment(id=i, start=start, end=words[-1].end, text=text, words=words)


@pytest.fixture
def raw():
    """Two takes of an intro, then talk broken up by long stretches of work."""
    return Transcript(
        video_id="raw-001",
        source="fixture",
        duration=200.0,
        word_timing="aligned",
        segments=[
            line(0, 1.0, "hey I'm Tom from"),  # abandoned take
            line(1, 10.0, "hey I'm Tom from Zero Vision Cinema"),
            line(2, 13.0, "today I thread a projector"),  # 0.6s pause before: same run
            line(3, 80.0, "that took forever"),  # 65s of work before
            line(4, 85.0, "now the reel"),  # 3.6s before: a short beat
            line(5, 120.0, "and it works"),  # 34s before: idle then show
        ],
    )


def keep():
    return [LineRange(first=1, last=5)]


def labels():
    return [
        PC.StretchLabel(start=14, end=80, kind="handwork", note="threading"),
        PC.StretchLabel(start=81, end=85, kind="repeat", note="spooling"),
        PC.StretchLabel(start=86, end=100, kind="idle", note="standing"),
        PC.StretchLabel(start=100, end=120, kind="show", note="first light"),
    ]


def test_stretches_are_the_long_gaps_between_consecutive_kept_lines(raw, rules):
    st = PC.find_stretches(raw, keep(), rules.longform.pacing)
    assert [(s.before, s.after) for s in st] == [(2, 3), (3, 4), (4, 5)]
    # the dropped take is not a stretch: lines 0 -> 1 are not both kept
    assert all(s.before != 0 for s in st)


def test_plan_speeds_by_kind_and_never_touches_speech(raw, rules):
    plan = PC.build_plan(raw, PC.PacingDraft(keep=keep(), labels=labels()), rules)
    assert not [i for i in plan.issues if i.severity == "error"], plan.issues
    kinds = [(s.kind, s.speed) for s in plan.segments]
    assert kinds == [
        ("speech", 1),  # lines 1-2, one run
        ("handwork", 8),  # 65s at 4x would be 16s > 15s max: stepped up to 8x
        ("speech", 1),
        ("repeat", 2),  # a short beat between lines stays gentle
        ("speech", 1),
        ("show", 1),
        ("speech", 1),
    ]
    assert plan.segments[0].lines == [1, 2]
    assert [d.reason for d in plan.dropped] == ["idle"]
    # the cut is the talk plus the sped work, nothing else
    talk = sum(s.duration for s in plan.segments if s.kind == "speech")
    assert plan.out_duration == pytest.approx(
        talk + plan.segments[1].duration / 8 + plan.segments[3].duration / 2 + 20, abs=0.1
    )


def test_unlabelled_parts_are_cut_and_reported(raw, rules):
    plan = PC.build_plan(raw, PC.PacingDraft(keep=keep(), labels=labels()[:2]), rules)
    codes = [i.code for i in plan.issues]
    assert "UNLABELLED_STRETCH" in codes
    assert any(d.reason == "unlabelled" and d.start < 90 < d.end for d in plan.dropped)


def test_still_frames_inside_work_are_cut(raw, rules):
    values = np.full(400, 0.1)
    values[60:80] = 0.0  # 30-40s: he left the frame
    plan = PC.build_plan(
        raw, PC.PacingDraft(keep=keep(), labels=labels()), rules, motion=PC.Motion(values)
    )
    work = [s for s in plan.segments if s.kind == "handwork"]
    assert len(work) == 2 and work[0].end == 30.0 and work[1].start == 40.0
    assert any(d.reason == "still frame" for d in plan.dropped)


def test_check_plan_catches_sped_speech_and_bad_speeds(raw, rules):
    plan = PC.build_plan(raw, PC.PacingDraft(keep=keep(), labels=labels()), rules)
    bad = plan.model_copy(deep=True)
    bad.segments[0].speed = 2
    bad.segments[1].speed = 3
    codes = {i.code for i in PC.check_plan(bad, raw, rules)}
    assert {"SPEECH_NOT_REALTIME", "SPEECH_SPED_UP", "LINE_CUT", "BAD_SPEED"} <= codes


def test_labels_outside_stretches_and_bad_lines_are_reported(raw, rules):
    draft = PC.PacingDraft(
        keep=[LineRange(first=1, last=9)],
        labels=[PC.StretchLabel(start=2, end=5, kind="setup", note="before the take")],
    )
    codes = {i.code for i in PC.build_plan(raw, draft, rules).issues}
    assert {"UNKNOWN_LINE", "LABEL_OUTSIDE_STRETCH"} <= codes


def test_pacing_rules_reject_unknown_kinds_and_speeds():
    with pytest.raises(ValueError):
        PacingRules(speeds={"idle": 0})
    sp = PacingRules().speeds | {"setup": 3}
    with pytest.raises(ValueError):
        PacingRules(speeds=sp)


def test_retimed_subtitles_and_render_script(raw, rules):
    plan = PC.build_plan(raw, PC.PacingDraft(keep=keep(), labels=labels()), rules)
    t = PC.retimed(plan, raw)
    assert [s.id for s in t.segments] == [1, 2, 3, 4, 5]
    assert t.segments[0].start == pytest.approx(plan.out_time(10.0))
    assert all(a.end <= b.start + 1e-6 for a, b in zip(t.segments, t.segments[1:], strict=False))
    assert plan.out_time(90.0) is None  # idle was cut
    sh = PC.render_script(plan, "src.mov", rules)
    assert sh.count("\nseg ") == len(plan.segments)
    assert "setpts=(PTS-STARTPTS)/8" in sh and "atempo=2.0,atempo=2.0,atempo=2" in sh


def test_draft_with_claude_sends_a_sheet_per_stretch(raw, rules, monkeypatch):
    monkeypatch.setattr(PC, "stretch_sheet", lambda video, st: b"jpeg")
    calls = []
    out = PC.PacingDraft(keep=keep(), labels=labels())

    class Messages:
        def parse(self, **kw):
            calls.append(kw)
            return SimpleNamespace(
                model=kw["model"], usage=SimpleNamespace(input_tokens=10, output_tokens=5),
                stop_reason="end_turn", parsed_output=out,
            )  # fmt: skip

    client = SimpleNamespace(beta=SimpleNamespace(messages=Messages()))
    run = Run(RunRecord(run_id="t", command="pace"))
    got = PC.draft_with_claude(rules, raw, keep(), "v.mp4", about="a vlog", run=run, client=client)
    assert got == out
    (kw,) = calls
    assert kw["output_format"] is PC.PacingDraft and "a vlog" in kw["system"]
    images = [c for c in kw["messages"][0]["content"] if c["type"] == "image"]
    assert len(images) == 3 and base64.b64decode(images[0]["source"]["data"]) == b"jpeg"
    assert "[1] 10.0-" in kw["messages"][0]["content"][0]["text"]


def test_cli_pace_from_draft_file(tmp_path, raw):
    (tmp_path / "t.json").write_text(raw.model_dump_json())
    draft = PC.PacingDraft(keep=keep(), labels=labels())
    (tmp_path / "d.json").write_text(json.dumps(draft.model_dump()))
    out = tmp_path / "out"
    res = CliRunner().invoke(
        app, ["pace", str(tmp_path / "t.json"), "--draft-file", str(tmp_path / "d.json"),
              "--out", str(out), "--render-source", "src.mov"],
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    assert "handwork 8x" in res.output
    plan = PC.EditPlan.model_validate_json((out / "edit_plan.json").read_text())
    assert len(plan.segments) == 7
    assert (out / "cut.srt").read_text().startswith("1\n00:00:00")
    assert 'SRC="src.mov"' in (out / "render.sh").read_text()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_motion_and_sheet_from_a_real_video(tmp_path):
    pytest.importorskip("cv2")
    v = tmp_path / "v.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=10:d=4",
         "-f", "lavfi", "-i", "color=c=gray:size=320x180:rate=10:d=4",
         "-filter_complex", "[0][1]concat=n=2", str(v)],
        check=True,
    )  # fmt: skip
    m = PC.Motion.from_video(v)
    assert m.values[2:7].mean() > 0.01
    ((a, b),) = m.still_runs(0, 8, 0.01, 2.0)  # the gray half
    assert 4.0 <= a <= 5.0 and b >= 7.0
    jpg = PC.stretch_sheet(v, PC.Stretch(start=0, end=8, before=0, after=1))
    assert jpg[:2] == b"\xff\xd8"
