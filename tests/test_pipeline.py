"""Per-clip subtitles, alignment helpers, and the CLI commands end to end."""

import json

import pytest
import yaml
from conftest import SMOKE_SET
from typer.testing import CliRunner

from cutroom import align
from cutroom.cli import app
from cutroom.schemas import ClipSegment, Segment, Transcript, Word
from cutroom.subtitles import SubtitleCorrection, SubtitleReview, clip_transcript


def W(text, a, b):
    return Word(text=text, start=a, end=b)


def _t(word_timing="aligned"):
    def seg(i, words):
        return Segment(id=i, start=words[0].start, end=words[-1].end,
                       text=" ".join(w.text for w in words), words=words)  # fmt: skip

    return Transcript(
        video_id="v", source="fixture", duration=100, word_timing=word_timing,
        segments=[
            seg(0, [W("First", 10.0, 10.4), W("line.", 10.5, 11.0)]),
            seg(1, [W("Skipped", 20.0, 20.5), W("line.", 20.6, 21.0)]),
            seg(2, [W("Last", 30.0, 30.3), W("line.", 30.4, 31.0)]),
        ],
    )  # fmt: skip


def test_clip_transcript_restitches_real_word_times(good_clip):
    clip = good_clip.model_copy(
        update={"segments": [ClipSegment(start=9.9, end=11.1), ClipSegment(start=29.8, end=31.2)]}
    )
    ct = clip_transcript(_t(), clip)
    assert [s.text for s in ct.segments] == ["First line.", "Last line."]  # middle line cut
    words = [(w.text, w.start, w.end) for w in ct.words()]
    # second part starts at 1.2s on the clip timeline (first part is 1.2s long)
    assert words == [
        ("First", 0.1, 0.5), ("line.", 0.6, 1.1), ("Last", 1.4, 1.7), ("line.", 1.8, 2.4)
    ]  # fmt: skip
    assert ct.duration == 2.6 and ct.video_id == f"{good_clip.video_id}/{good_clip.id}"


def test_clip_transcript_drops_lines_mostly_outside_the_cut(good_clip):
    clip = good_clip.model_copy(update={"segments": [ClipSegment(start=10.8, end=21.1)]})
    ct = clip_transcript(_t("estimated"), clip)
    assert [s.text for s in ct.segments] == ["Skipped line."]  # "First line." is 80% outside


def test_spoken_forms_for_alignment():
    assert align.spoken("post-Roe,") == ["post", "roe"]
    assert align.spoken("2026") == ["twenty", "twenty", "six"]
    assert align.spoken("Patty's") == ["patty's"]
    assert align.spoken("—") == []


def test_alignment_chunks_split_only_at_real_pauses():
    t = _t("estimated")
    # a long pause between line 0 and 1, only a short one between 1 and 2
    chunks = align._chunks(t, [(11.2, 19.5), (21.1, 21.2)], duration=40)
    assert chunks == [(0, 1, 0.0, 15.35), (1, 3, 15.35, 40)]


def test_align_needs_16k_mono(tmp_path):
    pytest.importorskip("pocketsphinx")
    import wave

    p = tmp_path / "a.wav"
    with wave.open(str(p), "w") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\0\0\0\0" * 100)
    with pytest.raises(ValueError, match="16 kHz mono"):
        align.align_transcript(_t("estimated"), p, [])


SRT = "1\n00:00:01,000 --> 00:00:02,500\nWe watched blade runner\n\n"


def test_cli_import_review_validate_queue(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    (tmp_path / "in.srt").write_text(SRT)
    res = runner.invoke(app, ["import-srt", "in.srt", "--id", "vid", "--duration", "10"])
    assert res.exit_code == 0, res.output
    t_path = tmp_path / "data" / "vid" / "transcript.resolve.json"
    assert json.loads(t_path.read_text())["segments"][0]["text"] == "We watched blade runner"

    review = SubtitleReview(
        corrections=[
            SubtitleCorrection(
                cue_id=0,
                original="We watched blade runner",
                corrected="We watched Blade Runner",
                kind="film_title",
                reason="film",
            )
        ]
    )
    (tmp_path / "review.json").write_text(review.model_dump_json())
    res = runner.invoke(app, ["review-subs", str(t_path), "--review-file", "review.json"])
    assert res.exit_code == 0, res.output
    out = tmp_path / "data" / "vid"
    assert "Blade Runner" in (out / "subtitles.reviewed.srt").read_text()
    reviewed = Transcript.model_validate_json((out / "transcript.reviewed.json").read_text())
    assert reviewed.source == "resolve_reviewed"

    # validate with video context: the end-card rules only run when it is given
    case = yaml.safe_load((SMOKE_SET / "cases" / "bad-missing-end-card.yaml").read_text())
    (tmp_path / "clip.yaml").write_text(yaml.safe_dump(case["clip"]))
    (tmp_path / "video.json").write_text(json.dumps(case["video"]))
    args = ["validate", "clip.yaml", "-t", str(SMOKE_SET / "transcript.json")]
    assert runner.invoke(app, args).exit_code == 0
    res = runner.invoke(app, [*args, "--video", "video.json"])
    assert res.exit_code == 1 and "MISSING_END_CARD" in res.output

    # queue lists what's in the store (it used to crash on a missing field)
    from cutroom.schemas import ClipProposal
    from cutroom.store import ClipStore

    ClipStore(tmp_path / "clips").put(ClipProposal.model_validate(case["clip"]))
    res = runner.invoke(app, ["queue", "--root", "clips"])
    assert res.exit_code == 0, res.output
    assert "bad-missing-end-card" in res.output and "vertical" in res.output
