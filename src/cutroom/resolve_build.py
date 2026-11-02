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
