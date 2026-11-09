"""Build vertical clip timelines in DaVinci Resolve Studio from a plan.json.

Runs anywhere the Resolve scripting API is reachable (`cutroom resolve-build`, or Resolve's
own script console). Everything Resolve-specific is in this file and goes through the
objects passed in, so it can be tested against a fake.

Per clip it builds a 1080x1920 30 fps timeline:
  V1  the source segments back to back, each Smart Reframed
  V2  the transparent caption overlay (word highlight)
  V3  the end card title, starting `overlap_frames` before the video ends
Existing timelines with the same name are replaced; anything else in the project is left alone.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

FPS = 30
SETTINGS = {
    "useCustomSettings": "1",
    "timelineFrameRate": str(FPS),
    "timelineResolutionWidth": "1080",
    "timelineResolutionHeight": "1920",
    # fill the 9:16 frame from the 16:9 source; Smart Reframe then follows the subject
    "timelineInputResMismatchBehavior": "scaleToCrop",
}


def timecode(frame: int, fps: int = FPS) -> str:
    return (
        f"{frame // (3600 * fps):02d}:{frame // (60 * fps) % 60:02d}:"
        f"{frame // fps % 60:02d}:{frame % fps:02d}"
    )


def _timelines(project: Any) -> dict[str, Any]:
    tls = (project.GetTimelineByIndex(i + 1) for i in range(project.GetTimelineCount()))
    return {t.GetName(): t for t in tls}


def _pool_item(root: Any, name: str) -> Any | None:
    return next((c for c in root.GetClipList() if c.GetName() == name), None)


def build_clip(
    project: Any,
    plan_entry: dict,
    source: Any,
    base_dir: Path,
    reframe: bool = True,
    wait: Callable[[float], None] = time.sleep,
) -> dict:
    """Build one clip timeline. Returns what was placed, for logging and checks."""
    mp = project.GetMediaPool()
    root = mp.GetRootFolder()
    mp.SetCurrentFolder(root)
    cid = plan_entry["id"]

    old = _timelines(project).get(cid)
    if old is not None:
        mp.DeleteTimelines([old])
    overlay_name = plan_entry["overlay"]
    stale = _pool_item(root, overlay_name)
    if stale is not None:
        mp.DeleteClips([stale])  # re-import so a re-rendered overlay is picked up
    overlay = mp.ImportMedia([str(base_dir / overlay_name)])[0]

    tl = mp.CreateEmptyTimeline(cid)
    project.SetCurrentTimeline(tl)
    for k, v in SETTINGS.items():
        tl.SetSetting(k, v)
    start = tl.GetStartFrame()

    # V1: source segments back to back (audio comes along on A1)
    rec = start
    infos = []
    for a, b in plan_entry["segments"]:
        sf, ef = round(a * FPS), round(b * FPS)
        infos.append(
            {"mediaPoolItem": source, "startFrame": sf, "endFrame": ef,
             "recordFrame": rec, "trackIndex": 1}
        )  # fmt: skip
        rec += ef - sf
    mp.AppendToTimeline(infos)
    video_end = rec
    while tl.GetTrackCount("video") < 3:
        tl.AddTrack("video")

    # V2: caption overlay, video only
    mp.AppendToTimeline(
        [{"mediaPoolItem": overlay, "startFrame": 0, "endFrame": video_end - start,
          "recordFrame": start, "trackIndex": 2, "mediaType": 1}]
    )  # fmt: skip

    # V3: end card. Inserting lands it on V1 at the playhead, so insert after the video
    # (nothing to overwrite there) and move it up, over the last frames.
    card = plan_entry.get("end_card")
    card_at = None
    if card:
        tl.SetCurrentTimecode(timecode(video_end))
        title = tl.InsertFusionTitleIntoTimeline(card["title"])
        if title is None:
            raise RuntimeError(f"title {card['title']!r} not found in Resolve's title library")
        card_at = video_end - card.get("overlap_frames", 0)
        if not tl.MoveItems([title], {"trackIndex": 3, "startFrame": card_at}):
            raise RuntimeError(f"could not move the end card onto V3 at frame {card_at}")

    reframed = 0
    if reframe:
        for item in tl.GetItemListInTrack("video", 1) or []:
            if item.SmartReframe():
                for _ in range(120):  # one job at a time; wait for it before the next
                    st = (project.GetSmartReframeStatus() or {}).get("JobStatus")
                    if st in ("Complete", "Failed", "Cancelled"):
                        break
                    wait(0.5)
                reframed += 1

    return {
        "id": cid,
        "video_frames": video_end - start,
        "segments": len(infos),
        "end_card_at": None if card_at is None else card_at - start,
        "reframed": reframed,
    }


def build_all(
    resolve: Any, plan_path: Path, source_path: Path, project_name: str | None = None, **kw: Any
) -> list[dict]:
    pm = resolve.GetProjectManager()
    project = pm.LoadProject(project_name) if project_name else pm.GetCurrentProject()
    if project is None and project_name:
        project = pm.CreateProject(project_name)
    if project is None:
        raise RuntimeError("no Resolve project open")
    mp = project.GetMediaPool()
    root = mp.GetRootFolder()
    source = _pool_item(root, source_path.name) or mp.ImportMedia([str(source_path)])[0]
    plan = json.loads(plan_path.read_text())
    done = [build_clip(project, entry, source, plan_path.parent, **kw) for entry in plan]
    pm.SaveProject()
    return done


def check_clip(project: Any, plan_entry: dict) -> list[str]:
    """Read a built timeline back and compare it with the plan. Returns problems found."""
    tl = _timelines(project).get(plan_entry["id"])
    if tl is None:
        return [f"{plan_entry['id']}: timeline missing"]
    start = tl.GetStartFrame()
    want = sum(round(b * FPS) - round(a * FPS) for a, b in plan_entry["segments"])
    problems = []
    v1 = tl.GetItemListInTrack("video", 1) or []
    v2 = tl.GetItemListInTrack("video", 2) or []
    v3 = tl.GetItemListInTrack("video", 3) or []
    got = max((i.GetEnd() for i in v1), default=start) - start
    if len(v1) != len(plan_entry["segments"]) or got != want:
        problems.append(f"V1 has {len(v1)} cuts / {got} frames, plan says "
                        f"{len(plan_entry['segments'])} / {want}")  # fmt: skip
    if len(v2) != 1 or v2[0].GetStart() != start or v2[0].GetEnd() - start != want:
        problems.append("V2 caption overlay doesn't span the video")
    card = plan_entry.get("end_card")
    if card:
        at = want - card.get("overlap_frames", 0)
        if len(v3) != 1 or v3[0].GetStart() - start != at:
            problems.append(f"V3 end card should start at frame {at}")
    return [f"{plan_entry['id']}: {p}" for p in problems]


# --------------------------------------------------------------------------- long-form edits

EDIT_COLORS = {2: "Yellow", 4: "Orange", 8: "Pink"}  # sped pieces, by speed; "show" is Teal


def build_edit_timeline(
    project: Any,
    plan: dict,
    source: Any,
    name: str,
    srt: str | None = None,
    width: int = 3840,
    height: int = 2160,
    sped_volume_db: float = -6.0,
    title_item: Any | None = None,
) -> dict:
    """Build a long-form edit (`pacing.EditPlan` as a dict) as one 16:9 timeline.

    Pieces go on V1/A1 back to back, then each sped piece gets its speed (pitch-corrected,
    rippling the timeline), its sound lowered, and a clip colour by speed so the edit reads at
    a glance. The retimed SRT, if given, goes on a subtitle track. An existing timeline with
    the same name is replaced.

    With the plan's title card and `title_item` (the per-video title timeline from
    `prepare_title`), the title plays as a nested timeline before its segment, so its video
    and every sound track stay in sync as one block.
    """
    mp = project.GetMediaPool()
    old = _timelines(project).get(name)
    if old is not None:
        mp.DeleteTimelines([old])
    tl = mp.CreateEmptyTimeline(name)
    for k, v in {**SETTINGS, "timelineResolutionWidth": str(width),
                 "timelineResolutionHeight": str(height)}.items():  # fmt: skip
        if k != "timelineInputResMismatchBehavior":
            tl.SetSetting(k, v)
    project.SetCurrentTimeline(tl)
    segs = plan["segments"]
    infos = [
        {"mediaPoolItem": source, "startFrame": round(s["start"] * FPS),
         "endFrame": round(s["end"] * FPS)}
        for s in segs
    ]  # fmt: skip
    card = plan.get("title_card")
    at = card["before_segment"] if card and title_item is not None else None
    if at is not None:
        infos.insert(at, {"mediaPoolItem": title_item})
    mp.AppendToTimeline(infos)
    sped = 0
    for i, s in enumerate(segs):
        k = i + (1 if at is not None and i >= at else 0)
        v, a = tl.GetItemListInTrack("video", 1)[k], tl.GetItemListInTrack("audio", 1)[k]
        if s["kind"] == "show":
            v.SetClipColor("Teal")
            a.SetClipColor("Teal")
        if s["speed"] == 1:
            continue
        if not v.SetSpeed(
            {"Percentage": s["speed"] * 100, "PitchCorrection": True, "RippleTimeline": True}
        ):
            raise RuntimeError(f"Resolve refused speed {s['speed']}x on piece {i}")
        a.SetProperty("AudioVolume", sped_volume_db)
        color = EDIT_COLORS.get(int(s["speed"]))
        if color:
            v.SetClipColor(color)
            a.SetClipColor(color)
        sped += 1
    subs = 0
    if srt:
        stem = srt.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        # Re-import every time: reusing an SRT already in the pool placed its cues at the
        # wrong rate (twice as late) in Resolve 21 when the pool item predates the timeline.
        known = _pool_item(mp.GetRootFolder(), stem)
        if known is not None:
            mp.DeleteClips([known])
        imported = mp.ImportMedia([srt])
        if imported:
            tl.AddTrack("subtitle")
            mp.AppendToTimeline([{"mediaPoolItem": imported[0], "recordFrame": tl.GetStartFrame()}])
            subs = len(tl.GetItemListInTrack("subtitle", 1) or [])
    return {"name": name, "pieces": len(segs), "sped": sped, "subtitles": subs,
            "title": at is not None, "frames": tl.GetEndFrame() - tl.GetStartFrame()}  # fmt: skip


def prepare_title(project: Any, template: str, name: str, text_tool: str, text: str) -> Any:
    """This video's copy of the title card: the template timeline (DRT) imported under `name`,
    with `text_tool`'s text replaced. Returns its media pool item, ready to nest."""
    mp = project.GetMediaPool()
    old = _timelines(project).get(name)
    if old is not None:
        mp.DeleteTimelines([old])
    tl = mp.ImportTimelineFromFile(template, {"timelineName": name, "importSourceClips": True})
    if tl is None:
        raise RuntimeError(f"could not import the title template {template}")
    tl.SetName(name)
    hits = 0
    for t in range(1, tl.GetTrackCount("video") + 1):
        for item in tl.GetItemListInTrack("video", t) or []:
            for c in range(1, item.GetFusionCompCount() + 1):
                tool = item.GetFusionCompByIndex(c).FindTool(text_tool)
                if tool is not None:
                    tool.SetInput("StyledText", text)
                    hits += 1
    if not hits:
        raise RuntimeError(f"no {text_tool!r} text in the title template")
    item = _pool_item(mp.GetRootFolder(), name)
    if item is None:
        raise RuntimeError(f"title timeline {name!r} not found in the media pool")
    return item


def check_edit_timeline(project: Any, plan: dict, name: str, slack_frames: int = 2) -> list[str]:
    """Read the timeline back: every piece present, at its speed, at its planned length."""
    tl = _timelines(project).get(name)
    if tl is None:
        return [f"{name}: timeline missing"]
    items = tl.GetItemListInTrack("video", 1)
    segs = plan["segments"]
    card = plan.get("title_card")
    if card and len(items) == len(segs) + 1:
        title = items.pop(card["before_segment"])
        if abs(title.GetDuration() - card["seconds"] * FPS) > FPS / 2:
            return [f"{name}: title card is {title.GetDuration()} frames, plan {card['seconds']}s"]
    if len(items) != len(segs):
        return [f"{name}: {len(items)} pieces on V1, plan has {len(segs)}"]
    out = []
    end = items[-1].GetEnd() if items else tl.GetStartFrame()
    subs = tl.GetItemListInTrack("subtitle", 1) or []
    if subs and max(x.GetEnd() for x in subs) > end + FPS // 2:  # cue ends are word ends
        out.append(f"{name}: subtitles run past the end of the video")
    for i, (it, s) in enumerate(zip(items, segs, strict=True)):
        want = (round(s["end"] * FPS) - round(s["start"] * FPS)) / s["speed"]
        if abs(it.GetDuration() - want) > slack_frames:
            out.append(f"{name}: piece {i} is {it.GetDuration()} frames, plan {want:.0f}")
        got = (it.GetSpeed() or {}).get("Percentage", 100.0)
        if abs(got - s["speed"] * 100) > 0.5:
            out.append(f"{name}: piece {i} plays at {got}%, plan {s['speed'] * 100:g}%")
    return out
