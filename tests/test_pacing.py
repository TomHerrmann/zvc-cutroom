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


class _Item:
    def __init__(self, tl, start, n):
        self.tl, self.start, self.n, self.speed, self.props, self.color = (
            tl,
            start,
            n,
            100.0,
            {},
            None,
        )

    def GetDuration(self):
        return self.n

    def GetEnd(self):
        return self.start + self.n

    def GetSpeed(self):
        return {"Percentage": self.speed}

    def SetSpeed(self, opts):
        new = round(self.n * self.speed / opts["Percentage"])
        shift, self.n, self.speed = new - self.n, new, opts["Percentage"]
        if opts.get("RippleTimeline"):
            for it in self.tl.items:
                if it is not self and it.start > self.start:
                    it.start += shift
        return True

    def SetProperty(self, k, v):
        self.props[k] = v
        return True

    def SetClipColor(self, c):
        self.color = c
        return True


class _Timeline:
    def __init__(self, name):
        self.name, self.items, self.settings, self.subs = name, [], {}, []

    def GetName(self):
        return self.name

    def SetSetting(self, k, v):
        self.settings[k] = v
        return True

    def GetStartFrame(self):
        return 108000

    def GetEndFrame(self):
        return 108000 + sum(i.n for i in self.items)

    def AddTrack(self, kind):
        return True

    def GetItemListInTrack(self, kind, i):
        if kind == "subtitle":
            return self.subs
        return sorted(self.items, key=lambda x: x.start)  # audio mirrors video here


class _Project:
    def __init__(self):
        self.tls, self.current = [], None
        proj = self

        class MP:
            def CreateEmptyTimeline(self, name):
                proj.tls.append(_Timeline(name))
                return proj.tls[-1]

            def DeleteTimelines(self, tls):
                proj.tls = [t for t in proj.tls if t not in tls]
                return True

            def GetRootFolder(self):
                return SimpleNamespace(GetClipList=lambda: [])

            def DeleteClips(self, items):
                return True

            def ImportMedia(self, paths):
                return [SimpleNamespace(name=p) for p in paths]

            def AppendToTimeline(self, infos):
                tl = proj.current
                for i in infos:
                    if "startFrame" in i:
                        tl.items.append(
                            _Item(tl, tl.GetEndFrame(), i["endFrame"] - i["startFrame"])
                        )
                    elif "recordFrame" not in i:  # a nested timeline: the title card
                        tl.items.append(_Item(tl, tl.GetEndFrame(), 189))
                    else:
                        tl.subs = [_Item(tl, tl.GetStartFrame() + k * 30, 30) for k in range(3)]
                return True

        self.mp = MP()

    def GetMediaPool(self):
        return self.mp

    def GetTimelineCount(self):
        return len(self.tls)

    def GetTimelineByIndex(self, i):
        return self.tls[i - 1]

    def SetCurrentTimeline(self, tl):
        self.current = tl
        return True


def test_resolve_edit_timeline_matches_the_plan(raw, rules):
    from cutroom import resolve_build as RB

    plan = PC.build_plan(raw, PC.PacingDraft(keep=keep(), labels=labels()), rules)
    data = plan.model_dump(mode="json")
    proj = _Project()
    done = RB.build_edit_timeline(proj, data, object(), "ep v1", srt="cut.srt")
    assert done["pieces"] == 7 and done["sped"] == 2 and done["subtitles"] == 3
    assert abs(done["frames"] / 30 - plan.out_duration) < 0.2
    assert RB.check_edit_timeline(proj, data, "ep v1") == []
    sped = [i for i in proj.tls[0].items if i.speed != 100]
    assert {i.color for i in sped} == {"Pink", "Yellow"} and sped[0].props["AudioVolume"] == -6
    # building again replaces the timeline; a wrong speed is caught on read-back
    RB.build_edit_timeline(proj, data, object(), "ep v1")
    assert len(proj.tls) == 1
    proj.tls[0].subs.append(_Item(proj.tls[0], proj.tls[0].GetEndFrame() + 900, 30))
    proj.tls[0].items[1].speed = 400.0
    problems = RB.check_edit_timeline(proj, data, "ep v1")
    assert any("plays at 400" in p for p in problems)
    assert any("subtitles run past" in p for p in problems)


def titled():
    return PC.PacingDraft(
        keep=keep(), labels=labels(), title=PC.TitleDraft(after_line=1, text="Raw: First Light")
    )


def test_title_card_goes_after_the_hook_and_shifts_the_cut(raw, rules):
    plain = PC.build_plan(raw, PC.PacingDraft(keep=keep(), labels=labels()), rules)
    plan = PC.build_plan(raw, titled(), rules)
    assert not [i for i in plan.issues if i.severity == "error"], plan.issues
    card = plan.title_card
    assert card.before_segment == 1 and card.text == "Raw: First Light"
    # line 1 now ends its own run, so the card sits between lines 1 and 2
    assert plan.segments[0].lines == [1] and plan.segments[1].lines == [2]
    secs = rules.longform.title_card.seconds
    # plus the card, minus the short pause between lines 1 and 2 that the split drops
    assert plain.out_duration + secs - 0.7 < plan.out_duration < plain.out_duration + secs
    assert plan.out_time(13.1) - plan.out_time(12.0) > secs
    sh = PC.render_script(plan, "src.mov", rules)
    assert "Raw: First Light" in sh  # noted: the review render leaves the card out


def test_title_card_must_follow_a_kept_line_and_fit(raw, rules):
    d = titled()
    d.title = PC.TitleDraft(after_line=0, text="x" * 60)
    plan = PC.build_plan(raw, d, rules)
    assert plan.title_card is None
    assert "TITLE_AFTER_UNKNOWN_LINE" in {i.code for i in plan.issues}
    d.title = PC.TitleDraft(after_line=1, text="x" * 60)
    assert "TITLE_TOO_LONG" in {i.code for i in PC.build_plan(raw, d, rules).issues}


def test_resolve_edit_nests_the_title_card(raw, rules):
    from cutroom import resolve_build as RB

    data = PC.build_plan(raw, titled(), rules).model_dump(mode="json")
    proj = _Project()
    done = RB.build_edit_timeline(proj, data, object(), "ep v2", title_item=object())
    assert done["title"] and len(proj.tls[0].items) == len(data["segments"]) + 1
    assert RB.check_edit_timeline(proj, data, "ep v2") == []
    # the sped piece after the card got its speed, not the card
    assert proj.tls[0].items[1].speed == 100.0 and proj.tls[0].items[3].speed == 800.0
