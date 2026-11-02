"""A deliberately simple clip store: one JSON file per clip under clips/<video_id>/.

Clip decisions live here, as data. Resolve projects are built *from* this store
and are never the source of truth.
"""

from __future__ import annotations

from pathlib import Path

from .schemas import ClipProposal, ClipStatus


class ClipStore:
    def __init__(self, root: Path | str = "clips"):
        self.root = Path(root)

    def _path(self, video_id: str, clip_id: str) -> Path:
        return self.root / video_id / f"{clip_id}.json"

    def put(self, clip: ClipProposal) -> Path:
        p = self._path(clip.video_id, clip.id)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(clip.model_dump_json(indent=2))
        return p

    def get(self, video_id: str, clip_id: str) -> ClipProposal:
        return ClipProposal.model_validate_json(self._path(video_id, clip_id).read_text())

    def list(
        self, video_id: str | None = None, status: ClipStatus | None = None
    ) -> list[ClipProposal]:
        pattern = f"{video_id}/*.json" if video_id else "*/*.json"
        clips = [
            ClipProposal.model_validate_json(p.read_text()) for p in sorted(self.root.glob(pattern))
        ]
        return [c for c in clips if status is None or c.status == status]

    def set_status(self, video_id: str, clip_id: str, status: ClipStatus) -> ClipProposal:
        clip = self.get(video_id, clip_id).model_copy(update={"status": status})
        self.put(clip)
        return clip
