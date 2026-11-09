"""The music bed under sped-up work: runs, fades, ducking, and where it lands."""

import json
import wave

import numpy as np
import pytest
from test_pacing import _Project, keep, labels, raw, titled  # noqa: F401
from typer.testing import CliRunner

from cutroom import music as M
from cutroom import pacing as PC
from cutroom.cli import app
from cutroom.config import MusicRules, MusicTrack


def seg(start, end, speed=1.0, kind="speech"):
    return PC.EditSegment(start=start, end=end, speed=speed, kind=kind)


def plan(*segs, title_before=None):
    card = (
        PC.TitleCard(before_segment=title_before, after_line=0, text="T", seconds=6.0,
                     template="t.drt")
        if title_before is not None else None
    )  # fmt: skip
    return PC.EditPlan(video_id="v", source_duration=999, segments=list(segs), title_card=card)


R = MusicRules(merge_gap_seconds=4, fade_in_seconds=0.5, fade_out_seconds=1.0,
               duck_ramp_seconds=0.25, bed_db=-4, duck_db=-12)  # fmt: skip


def test_layout_matches_render_sh_frame_rounding():
    p = plan(seg(0, 10), seg(10, 50, 8, "handwork"), seg(50, 51.01))
    pcs = M.layout(p, 30)
    assert [round(x.end - x.start, 4) for x in pcs] == [10.0, 5.0, 1.0]
    assert [x.sped for x in pcs] == [False, True, False]
    # the review render leaves the title card out; the Resolve timeline has it
    pt = plan(seg(0, 10), seg(10, 50, 8, "handwork"), title_before=1)
    assert [x.kind for x in M.layout(pt, include_title=True)] == ["speech", "title", "handwork"]
    assert [x.kind for x in M.layout(pt, include_title=False)] == ["speech", "handwork"]


def test_short_lines_between_sped_pieces_keep_the_music_going_ducked():
    p = plan(
        seg(0, 10),
        seg(10, 50, 8, "handwork"),  # 5s at 10-15
        seg(50, 52),  # a 2s line: ducked, music carries on
        seg(52, 76, 4, "repeat"),  # 6s at 17-23
        seg(76, 96),  # 20s of talking: the run ends
        seg(96, 112, 8, "setup"),  # 2s at 43-45
        seg(112, 120),
    )
    cues = M.music_cues(M.layout(p), R)
    assert len(cues) == 2
    a, b = cues
    assert a.segments == [1, 3] and (a.start, a.end) == (10.0, 24.0)  # fade tails 1s into talk
    lvl = dict(a.points)
    assert lvl[10.0] == M.SILENT_DB and lvl[10.5] == -4
    assert lvl[15.25] == -16 and lvl[17.25] == -4  # ducked under the line, back up after
    assert a.points[-2] == (23.0, -4) and a.points[-1] == (24.0, M.SILENT_DB)
    assert b.segments == [5] and (b.start, b.end) == (43.0, 46.0)
    env = M.envelope(a, 1000)
    assert env[0] == 0 and env[-1] < 0.02
    assert env[3000] == pytest.approx(10 ** (-4 / 20)) and env[6000] == pytest.approx(10**-0.8)


def test_title_card_and_end_of_video_cut_the_fade_short():
    p = plan(seg(0, 10), seg(10, 50, 8, "handwork"), seg(50, 52), title_before=2)
    (c,) = M.music_cues(M.layout(p), R)
    # it fades out by the title card, not under it
    assert c.end == 15.0 and c.points[-1] == (15.0, M.SILENT_DB) and c.points[-2][0] == 14.0
    (c,) = M.music_cues(M.layout(plan(seg(0, 10), seg(10, 50, 8, "repeat"))), R)
    assert c.end == 15.0


def test_bed_continues_the_music_from_run_to_run():
    p = plan(seg(0, 10), seg(10, 50, 8, "handwork"), seg(50, 70), seg(70, 110, 8, "repeat"),
             seg(110, 120))  # fmt: skip
    pcs = M.layout(p)
    cues = M.music_cues(pcs, R)
    sr = 100
    ramp = np.linspace(0, 1, 100 * sr, dtype=np.float32)
    track = np.stack([ramp, ramp], axis=1)  # value = position in the music
    bed, placed = M.build_bed(cues, [track], pcs[-1].end, R, sr=sr)
    assert len(bed) == round(pcs[-1].end * sr)
    assert placed[0]["music_from"] == 0 and placed[1]["music_from"] == pytest.approx(6.0)
    assert np.all(bed[: 10 * sr] == 0) and np.all(bed[17 * sr : 30 * sr] == 0)
    # mid-run the bed is the music at bed level, picked up where the last run stopped
    t = 37 * sr
    want = (6.0 + 2.0) / 100 * 10 ** (-4 / 20)
    assert bed[t, 0] == pytest.approx(want, rel=1e-3)


def test_playlist_crossfades_and_loops():
    a, b = np.ones((100, 2), np.float32), np.zeros((100, 2), np.float32)
    out = M.playlist([a, b], 350, crossfade=0.1, sr=100)
    assert len(out) >= 350 and out[95, 0] < 1 and out[150, 0] == 0 and out[250, 0] == 1


def test_render_script_mixes_the_bed_when_there_is_one(raw, rules):  # noqa: F811
    sh = PC.render_script(PC.build_plan(raw, titled(), rules), "src.mov", rules)
    assert "music_bed.wav" in sh and "amix=inputs=2:duration=first:normalize=0" in sh


def test_resolve_puts_the_bed_on_its_own_track(raw, rules):  # noqa: F811
    from cutroom import resolve_build as RB

    data = PC.build_plan(raw, PC.PacingDraft(keep=keep(), labels=labels()), rules).model_dump(
        mode="json"
    )
    proj = _Project()
    done = RB.build_edit_timeline(proj, data, object(), "ep m", music_item="bed")
    tl = proj.tls[0]
    assert done["music"] and tl.track_names[("audio", 2)] == "Music"
    (m,) = tl.music
    assert m["trackIndex"] == 2 and m["mediaType"] == 2 and m["recordFrame"] == 108000
    assert m["endFrame"] == done["frames"] - 1
    assert RB.check_edit_timeline(proj, data, "ep m") == []


def test_cli_music_writes_bed_credits_and_cues(tmp_path, raw, rules):  # noqa: F811
    import yaml

    from cutroom.config import DEFAULT_RULES_PATH

    track = tmp_path / "lib" / "song.wav"
    track.parent.mkdir()
    with wave.open(str(track), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(48000)
        t = np.arange(48000 * 20) / 48000
        tone = (0.3 * np.sin(2 * np.pi * 220 * t) * 32767).astype("<i2")
        w.writeframes(np.repeat(tone, 2).tobytes())
    cfg = yaml.safe_load(DEFAULT_RULES_PATH.read_text())
    cfg["longform"]["music"] = {
        "tracks": [MusicTrack(file="lib/song.wav", title="Song", artist="Someone",
                              source_url="https://pixabay.com/music/x-1/",
                              license="Pixabay Content License").model_dump()]
    }  # fmt: skip
    rp = tmp_path / "rules.yaml"
    rp.write_text(yaml.safe_dump(cfg))
    p = PC.build_plan(raw, titled(), rules)
    pp = tmp_path / "ep" / "edit_plan.json"
    PC.save(p, pp)
    res = CliRunner().invoke(app, ["music", str(pp), "--zvc-root", str(tmp_path),
                                   "--rules", str(rp)])  # fmt: skip
    assert res.exit_code == 0, res.output
    for name, titled_ in (("music_bed", False), ("music_bed_titled", True)):
        info = json.loads((pp.parent / f"{name}.json").read_text())
        assert info["include_title"] is titled_ and info["cues"]
        with wave.open(str(pp.parent / f"{name}.wav")) as w:
            assert w.getframerate() == 48000 and w.getnchannels() == 2
            assert abs(w.getnframes() / 48000 - info["seconds"]) < 0.01
    plain = json.loads((pp.parent / "music_bed.json").read_text())["seconds"]
    with_card = json.loads((pp.parent / "music_bed_titled.json").read_text())["seconds"]
    assert with_card - plain == pytest.approx(p.title_card.seconds, abs=0.04)
    cred = (pp.parent / "music_credits.md").read_text()
    assert "https://pixabay.com/music/x-1/" in cred and "Pixabay Content License" in cred
