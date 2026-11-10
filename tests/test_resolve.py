"""Resolve assembly, against a fake that records what the real API would be asked to do.

The fake follows the behaviour seen in Resolve Studio 21: AppendToTimeline honours
recordFrame and trackIndex, and InsertFusionTitleIntoTimeline drops the title on V1 at the
playhead (overwriting whatever is there), which is why the builder moves it afterwards.
"""

import json
import subprocess

import pytest

from cutroom import resolve_build
from cutroom.render import overlay_command, render_overlays

START = 108000  # 01:00:00:00 at 30 fps


class Item:
    def __init__(self, name, start, end, kind="video"):
        self.name, self.start, self.end, self.kind = name, start, end, kind
        self.reframed = False

    def GetName(self):
        return self.name

    def GetStart(self):
        return self.start

    def GetEnd(self):
        return self.end

    def SmartReframe(self):
        self.reframed = True
        return True


class PoolItem:
    def __init__(self, name):
        self.name = name

    def GetName(self):
        return self.name


class Timeline:
    def __init__(self, name, titles):
        self.name, self.titles = name, titles
        self.settings, self.tracks, self.playhead = {}, {1: []}, START

    def GetName(self):
        return self.name

    def SetSetting(self, k, v):
        self.settings[k] = v
        return True

    def GetStartFrame(self):
        return START

    def GetTrackCount(self, kind):
        return len(self.tracks)

    def AddTrack(self, kind):
        self.tracks[len(self.tracks) + 1] = []
        return True

    def GetItemListInTrack(self, kind, i):
        return sorted(self.tracks.get(i, []), key=lambda x: x.start)

    def SetCurrentTimecode(self, tc):
        h, m, s, f = (int(x) for x in tc.split(":"))
        self.playhead = ((h * 60 + m) * 60 + s) * 30 + f
        return True

    def InsertFusionTitleIntoTimeline(self, name):
        if name not in self.titles:
            return None
        item = Item(name, self.playhead, self.playhead + 90)
        self.tracks[1].append(item)
        return item

    def MoveItems(self, items, pos):
        for it in items:
            for t in self.tracks.values():
                if it in t:
                    t.remove(it)
            n = it.end - it.start
            it.start, it.end = int(pos["startFrame"]), int(pos["startFrame"]) + n
            self.tracks[pos["trackIndex"]].append(it)
        return True


class MediaPool:
    def __init__(self, project):
        self.project, self.pool, self.deleted = project, [], []

    def GetRootFolder(self):
        return self

    def GetClipList(self):
        return list(self.pool)

    def SetCurrentFolder(self, f):
        return True

    def ImportMedia(self, paths):
        items = [PoolItem(p.rsplit("/", 1)[-1]) for p in paths]
        self.pool += items
        return items

    def DeleteClips(self, items):
        self.pool = [c for c in self.pool if c not in items]
        return True

    def DeleteTimelines(self, tls):
        self.deleted += [t.name for t in tls]
        self.project.tls = [t for t in self.project.tls if t not in tls]
        return True

    def CreateEmptyTimeline(self, name):
        tl = Timeline(name, self.project.titles)
        self.project.tls.append(tl)
        return tl

    def AppendToTimeline(self, infos):
        tl = self.project.current
        out = []
        for i in infos:
            n = i["endFrame"] - i["startFrame"]
            it = Item(i["mediaPoolItem"].GetName(), i["recordFrame"], i["recordFrame"] + n)
            tl.tracks.setdefault(i["trackIndex"], []).append(it)
            out.append(it)
        return out


class Project:
    def __init__(self, titles=("ZVC Topic Label Slam",)):
        self.titles, self.tls, self.current = set(titles), [], None
        self.mp = MediaPool(self)

    def GetMediaPool(self):
        return self.mp

    def GetTimelineCount(self):
        return len(self.tls)

    def GetTimelineByIndex(self, i):
        return self.tls[i - 1]

    def SetCurrentTimeline(self, tl):
        self.current = tl
        return True

    def GetSmartReframeStatus(self):
        return {"JobStatus": "Complete"}


ENTRY = {
    "id": "v1",
    "segments": [[0.08, 8.155], [333.585, 348.335], [42.69, 47.595]],
    "duration": 27.7,
    "overlay": "v1.captions.mov",
    "end_card": {"title": "ZVC Topic Label Slam", "overlap_frames": 12},
}


def test_builds_the_vertical_timeline_layout(tmp_path):
    proj = Project()
    source = PoolItem("source.mp4")
    out = resolve_build.build_clip(proj, ENTRY, source, tmp_path, wait=lambda s: None)
    tl = proj.tls[0]
    assert tl.settings["timelineResolutionWidth"] == "1080"
    assert tl.settings["timelineResolutionHeight"] == "1920"
    assert tl.settings["timelineInputResMismatchBehavior"] == "scaleToCrop"
    v1 = tl.GetItemListInTrack("video", 1)
    # cuts sit back to back, in plan order, frame-accurate
    assert [(i.start - START, i.end - START) for i in v1] == [(0, 243), (243, 685), (685, 832)]
    assert all(i.reframed for i in v1) and out["reframed"] == 3
    (cap,) = tl.GetItemListInTrack("video", 2)
    assert (cap.name, cap.start - START, cap.end - START) == ("v1.captions.mov", 0, 832)
    (card,) = tl.GetItemListInTrack("video", 3)
    assert card.start - START == 832 - 12  # slams in over the last 12 frames
    assert resolve_build.check_clip(proj, ENTRY) == []


def test_rebuild_replaces_only_its_own_timeline(tmp_path):
    proj = Project()
    keep = proj.mp.CreateEmptyTimeline("v1 (your title edit)")
    source = PoolItem("source.mp4")
    resolve_build.build_clip(proj, ENTRY, source, tmp_path, wait=lambda s: None)
    resolve_build.build_clip(proj, ENTRY, source, tmp_path, wait=lambda s: None)
    assert proj.mp.deleted == ["v1"]
    assert [t.name for t in proj.tls] == ["v1 (your title edit)", "v1"] and keep in proj.tls
    assert [c.name for c in proj.mp.pool].count("v1.captions.mov") == 1


def test_missing_title_fails_loudly(tmp_path):
    proj = Project(titles=())
    with pytest.raises(RuntimeError, match="title library"):
        resolve_build.build_clip(proj, ENTRY, PoolItem("s.mp4"), tmp_path, wait=lambda s: None)


def test_check_catches_a_misplaced_end_card(tmp_path):
    proj = Project()
    resolve_build.build_clip(proj, ENTRY, PoolItem("s.mp4"), tmp_path, wait=lambda s: None)
    proj.tls[0].tracks[3][0].start += 12  # someone dragged it to start after the video
    assert resolve_build.check_clip(proj, ENTRY) == ["v1: V3 end card should start at frame 820"]


def test_timecode():
    assert resolve_build.timecode(START + 832) == "01:00:27:22"


def test_overlay_render_command_and_failure(tmp_path):
    cmd = overlay_command(tmp_path / "v1.ass", 27.7, tmp_path / "v1.captions.mov", None)
    assert (
        cmd[cmd.index("-c:v") + 1] == "qtrle"
    )  # Resolve reads its alpha; ProRes 4444's it ignored
    assert "d=27.7" in cmd[cmd.index("-i") + 1] and cmd[-1] == "v1.captions.mov"
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps([{"id": "v1", "captions": "v1.ass", "duration": 27.7,
                                 "overlay": "v1.captions.mov"}]))  # fmt: skip
    calls = []

    def ok(cmd, **kw):
        calls.append(kw["cwd"])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    assert render_overlays(plan, runner=ok) == [tmp_path / "v1.captions.mov"]
    assert calls == [tmp_path]

    def bad(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, "", "No such filter: 'ass'")

    with pytest.raises(RuntimeError, match="No such filter"):
        render_overlays(plan, runner=bad)


def test_timeline_names_follow_the_convention(tmp_path):
    from cutroom.cli import edit_version
    from cutroom.config import load_rules

    tn = load_rules().timelines
    assert tn.long_name("Breeder Review", 1) == "Breeder Review long v1"
    assert tn.long_name("16mm-journey-ep1", 2) == "16mm-journey-ep1 long v2"
    assert tn.title_name("16mm-journey-ep1 long v2") == "16mm-journey-ep1 long v2 title"
    assert tn.vertical_name("Breeder Review", "ebay-gamble") == "Breeder Review ebay-gamble"
    d = tmp_path / "longform-v3"
    d.mkdir()
    assert edit_version(d / "edit_plan.json") == 3
    assert edit_version(tmp_path / "edit_plan.json") == 1


def test_clip_timeline_can_take_a_conventional_name(tmp_path):
    proj, source = Project(), PoolItem("source.mp4")
    resolve_build.build_clip(proj, ENTRY, source, tmp_path, wait=lambda s: None,
                             name="Breeder Review v1")  # fmt: skip
    assert resolve_build.check_clip(proj, ENTRY, "Breeder Review v1") == []
    assert resolve_build.check_clip(proj, ENTRY) == ["v1: timeline missing"]
