"""Milestone 2: drafts from Claude become clips only through code and the validators."""

import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from cutroom import propose as P
from cutroom.cli import app
from cutroom.cuts import cut_lines
from cutroom.runlog import Run
from cutroom.schemas import ClipProposal, ClipSegment, ClipStatus, RunRecord, VideoContext
from cutroom.store import ClipStore


@pytest.fixture
def video(transcript):
    """Silences in every gap between lines, a long review on YouTube."""
    segs = transcript.segments
    gaps = [(a.end, b.start) for a, b in zip(segs, segs[1:], strict=False)]
    return VideoContext(
        video_id=transcript.video_id,
        silences=[(0.0, 0.0)] + gaps + [(segs[-1].end, transcript.duration)],
        long_review="Full review",
    )


def moment(**over):
    base = dict(
        slug="practical-effects",
        lines=[P.LineRange(first=2, last=4)],
        theme="practical effects",
        hook="Practical effects age better",
        caption="Why practical effects hold up. Full review on YouTube.",
        hashtags=["#zerovisioncinema", "#practicaleffects"],
        title_variants=["Practical effects age better"],
        quotes=[P.DraftQuote(first_line=3, last_line=3, text="because the light is real")],
        cover_line=3,
        cover_text="Practical effects age better",
        scores=P.DraftScores(hook=0.8, quote_punch=0.7, theme=0.9),
        why="A clear, quotable claim.",
    )
    base.update(over)
    return P.MomentDraft(**base)


class FakeFaces:
    def __init__(self, face=True):
        self.face = face

    def has_face(self, t):
        return self.face

    def share(self, segments, step=2.0):
        return 1.0 if self.face else 0.0


def test_line_ranges_become_cuts_in_the_pauses(transcript, video, rules):
    clip, issues = P.compile_draft(moment(), transcript, rules, video, faces=FakeFaces())
    assert issues == [] and clip is not None
    (seg,) = clip.segments
    # line 2 starts at 40.0, line 4 ends at 56.4: both edges land in the measured gaps
    assert 12.8 <= seg.start <= 40.0 and 56.4 <= seg.end <= 60.0
    assert video.in_silence(seg.start) and video.in_silence(seg.end)
    # quote times come from the cited line, not from the model
    line3 = transcript.segments[3]
    assert (clip.quotes[0].start, clip.quotes[0].end) == (line3.start, line3.end)
    assert clip.created_by == "llm" and clip.end_card == rules.end_card.topic_title
    assert clip.cta.kind == "overlay" and clip.cta.at <= rules.cta.max_start_seconds
    assert clip.covers[0].has_face is True and clip.contains(clip.covers[0].frame_time)
    assert clip.scores.face_on_screen == 1.0 and clip.scores.length_fit is not None


def test_unknown_or_backwards_lines_never_become_a_clip(transcript, rules):
    clip, issues = P.compile_draft(moment(lines=[P.LineRange(first=2, last=99)]), transcript, rules)
    assert clip is None and [i.code for i in issues] == ["UNKNOWN_LINE"]
    clip, issues = P.compile_draft(moment(lines=[P.LineRange(first=4, last=2)]), transcript, rules)
    assert clip is None and [i.code for i in issues] == ["BAD_LINE_RANGE"]


def test_invented_quote_is_rejected_by_the_validator(transcript, video, rules):
    bad = moment(quotes=[P.DraftQuote(first_line=3, last_line=3, text="the light is fake")])
    res = P.check_draft(bad, transcript, rules, video, faces=FakeFaces())
    assert not res.passed
    assert "QUOTE_NOT_IN_TRANSCRIPT" in res.report.error_codes
    assert res.clip.status == ClipStatus.REJECTED_BY_VALIDATOR


def test_cover_without_a_face_fails(transcript, video, rules):
    res = P.check_draft(moment(), transcript, rules, video, faces=FakeFaces(face=False))
    assert "COVER_NO_FACE" in res.report.error_codes


def test_repair_round_fixes_a_failing_draft(transcript, video, rules):
    good = moment()
    bad = moment(quotes=[P.DraftQuote(first_line=3, last_line=3, text="the light is fake")])
    seen = []

    def repair(failed):
        seen.append(failed)
        return P.ProposalDraft(moments=[good])

    draft = P.ProposalDraft(moments=[bad])
    (res,) = P.propose(rules, transcript, draft, video, faces=FakeFaces(), repair=repair)
    assert res.passed and res.attempts == 2
    assert res.clip.status == ClipStatus.PENDING_REVIEW
    # the repair call was told exactly what failed
    ((sent_draft, issues),) = seen[0]
    assert sent_draft.slug == "practical-effects"
    assert "QUOTE_NOT_IN_TRANSCRIPT" in [i.code for i in issues]
    assert "QUOTE_NOT_IN_TRANSCRIPT" in P.repair_prompt(seen[0])


def test_results_rank_passing_first_and_ids_stay_unique(transcript, video, rules):
    low = moment(scores=P.DraftScores(hook=0.1, quote_punch=0.1, theme=0.1))
    high = moment()
    broken = moment(slug="broken", lines=[P.LineRange(first=50, last=51)])
    res = P.propose(
        rules, transcript, P.ProposalDraft(moments=[low, broken, high]), video, faces=FakeFaces()
    )
    assert [r.passed for r in res] == [True, True, False]
    assert res[0].rank > res[1].rank
    assert {r.clip.id for r in res[:2]} == {"vert-practical-effects", "vert-practical-effects-2"}
    assert res[2].clip is None


def test_length_fit_and_rank(rules):
    lo, hi = rules.proposals.target_seconds
    assert P.length_fit((lo + hi) / 2, rules) == 1.0
    assert P.length_fit(rules.formats["vertical"].max_seconds, rules) == 0.0
    assert 0 < P.length_fit(hi + 5, rules) < 1
    from cutroom.schemas import MomentScores

    # scores that are missing don't count against a clip
    assert P.rank_score(MomentScores(hook=0.5), rules) == 0.5
    assert P.rank_score(MomentScores(), rules) == 0.0


def test_unknown_weight_is_a_config_error():
    from cutroom.config import ProposalRules

    with pytest.raises(ValueError, match="unknown score weights"):
        ProposalRules(weights={"vibes": 1.0})


def test_prompt_carries_the_brand_rules_and_line_marks(transcript, video, rules):
    v = video.model_copy(update={"youtube_only": [(8.0, 13.0)]})
    system = P._system_prompt(rules, v, ["narrator was dead"])
    assert "#zerovisioncinema" in system and "3-5 words" in system
    assert rules.end_card.topic_title in system and "narrator was dead" in system
    user = P.transcript_prompt(rules, transcript, v)
    assert "[1] 8.00-12.80" in user and "(LONG-FORM ONLY" in user
    assert "(SIGN-OFF" in user.splitlines()[-1]  # "thanks for watching"
    assert "About this video: a vlog" in P._system_prompt(rules, v, [], "a vlog")
    assert "review" not in system.split("Videos come")[0]  # framing isn't review-only
    # no long review: no end card rule
    assert rules.end_card.topic_title not in P._system_prompt(rules, None, [])


def test_run_ons_mark_lines_with_no_pause_after(transcript, video):
    # close the gap between lines 3 and 4: line 3 can no longer end a cut
    silences = [s for s in video.silences if s != (50.2, 52.0)]
    v = video.model_copy(update={"silences": silences})
    assert P.run_ons(transcript, v) == {3}
    assert P.run_ons(transcript, None) == set()


def test_cut_lines_without_an_audio_map(transcript, video):
    seg, clean = cut_lines(transcript, [2], video=video)
    assert clean and video.in_silence(seg.start) and video.in_silence(seg.end)
    seg, clean = cut_lines(transcript, [2])
    assert not clean and seg.start < 40.0 < 43.6 < seg.end


def test_match_picks_against_human_clips():
    def c(cid, *ranges):
        return ClipProposal(
            id=cid, video_id="v", hook="h", caption="c",
            segments=[ClipSegment(start=a, end=b) for a, b in ranges],
        )  # fmt: skip

    human = [c("h1", (10, 20), (50, 60)), c("h2", (100, 110))]
    auto = [c("a1", (50, 60), (10, 20)), c("a2", (200, 210))]
    assert P.overlap(auto[0], human[0]) == 1.0  # order of segments doesn't matter
    matches, recall = P.match_picks(auto, human)
    assert [(m.human, m.auto, m.auto_rank) for m in matches] == [
        ("h1", "a1", 1),
        ("h2", None, None),
    ]
    assert recall == 0.5


def test_draft_with_claude_sends_typed_request(transcript, video, rules):
    calls = []
    out = P.ProposalDraft(moments=[moment()])

    class Messages:
        def parse(self, **kw):
            calls.append(kw)
            return SimpleNamespace(
                model=kw["model"],
                usage=SimpleNamespace(input_tokens=1000, output_tokens=200),
                stop_reason="end_turn",
                parsed_output=out,
            )

    client = SimpleNamespace(beta=SimpleNamespace(messages=Messages()))
    run = Run(RunRecord(run_id="t", command="propose"))
    got = P.draft_with_claude(rules, transcript, video, run=run, client=client)
    assert got == out
    (kw,) = calls
    assert kw["output_format"] is P.ProposalDraft and kw["model"] == rules.proposals.model
    assert kw["output_config"] == {"effort": rules.proposals.effort}
    assert "[2] 40.00-43.60" in kw["messages"][0]["content"]
    assert run.record.input_tokens == 1000 and run.record.cost_usd > 0


def test_refusal_and_truncation_raise(transcript, rules):
    for stop in ("refusal", "max_tokens"):
        resp = SimpleNamespace(
            model="m", usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            stop_reason=stop, parsed_output=None,
        )  # fmt: skip
        client = SimpleNamespace(
            beta=SimpleNamespace(messages=SimpleNamespace(parse=lambda resp=resp, **kw: resp))
        )
        with pytest.raises(RuntimeError):
            P.draft_with_claude(rules, transcript, client=client)


def test_cli_propose_from_draft_file(tmp_path, monkeypatch, transcript, video):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "t.json").write_text(transcript.model_dump_json())
    (tmp_path / "v.json").write_text(video.model_dump_json())
    store = ClipStore(tmp_path / "clips")
    # a human pick to compare against, and a stale machine proposal that should be replaced
    human = ClipProposal(
        id="h1", video_id=transcript.video_id, hook="h", caption="c",
        segments=[ClipSegment(start=39.9, end=56.5)],
    )  # fmt: skip
    store.put(human)
    store.put(human.model_copy(update={"id": "auto-stale", "created_by": "llm"}))
    draft = P.ProposalDraft(moments=[moment()])
    (tmp_path / "d.json").write_text(json.dumps(draft.model_dump()))
    res = CliRunner().invoke(
        app,
        ["propose", "t.json", "--video", "v.json", "--draft-file", "d.json",
         "--root", str(tmp_path / "clips")],
    )  # fmt: skip
    # no source video, so the cover face is unverified and the clip is rejected
    assert res.exit_code == 0, res.output
    assert "COVER_NO_FACE" in res.output and "h1               -> no match" in res.output
    ids = {c.id for c in store.list(transcript.video_id)}
    assert ids == {"h1", "vert-practical-effects"}
    assert store.get(transcript.video_id, "vert-practical-effects").status == (
        ClipStatus.REJECTED_BY_VALIDATOR
    )


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_face_probe_on_a_faceless_video(tmp_path):
    pytest.importorskip("cv2")
    from cutroom.faces import FaceProbe

    mp4 = tmp_path / "bars.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10",
         "-t", "3", str(mp4)],
        check=True,
    )  # fmt: skip
    probe = FaceProbe(mp4)
    assert probe.has_face(1.0) is False
    assert probe.share([ClipSegment(start=0, end=3)], step=1.0) == 0.0
    assert probe.has_face(99.0) is None  # past the end: unknown, not "no face"
    probe.close()


def test_long_form_only_lines_cannot_be_cut(transcript, video, rules):
    v = video.model_copy(update={"youtube_only": [(7.5, 13.0)]})
    clip, issues = P.compile_draft(
        moment(lines=[P.LineRange(first=1, last=1)]), transcript, rules, v
    )
    assert clip is None and [i.code for i in issues] == ["LONG_FORM_ONLY_LINE"]


def test_cover_moves_to_the_nearest_frame_with_a_face(transcript, video, rules):
    class FaceAfter:
        def has_face(self, t):
            return t >= 54.0

        def share(self, segments, step=2.0):
            return 0.25

    clip, _ = P.compile_draft(moment(cover_line=2), transcript, rules, video, faces=FaceAfter())
    cover = clip.covers[0]
    assert cover.has_face is True and 54.0 <= cover.frame_time < 54.5
    # no face anywhere in the clip: the cover stays where the model put it and fails the check
    res = P.check_draft(moment(), transcript, rules, video, faces=FakeFaces(face=False))
    assert res.clip.covers[0].has_face is False
