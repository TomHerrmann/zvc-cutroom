"""Is there a face on screen? Answers the cover check and the face_on_screen score.

OpenCV's bundled frontal-face Haar cascade, run on frames read straight from the source
video. It is a coarse detector, so it only answers "is a face clearly in frame", and only a
face big enough to read on a phone counts. Needs the `faces` extra (opencv-python-headless).
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from .schemas import ClipSegment

# A face must be at least this share of the frame height to count (a phone-readable face,
# not someone in the background).
MIN_FACE_SHARE = 0.12


class FaceProbe:
    def __init__(self, video: Path):
        import cv2  # optional dependency

        self._cv2 = cv2
        self._cap = cv2.VideoCapture(str(video))
        if not self._cap.isOpened():
            raise ValueError(f"can't open {video}")
        self._cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )
        self._cache: dict[float, bool] = {}

    def has_face(self, t: float) -> bool | None:
        """True/False for the frame at source time t; None if the frame can't be read."""
        key = round(t, 2)
        if key in self._cache:
            return self._cache[key]
        cv2 = self._cv2
        self._cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, frame = self._cap.read()
        if not ok:
            return None
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        h = gray.shape[0]
        side = int(h * MIN_FACE_SHARE)
        found = self._cascade.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=6, minSize=(side, side)
        )
        self._cache[key] = len(found) > 0
        return self._cache[key]

    def share(self, segments: Iterable[ClipSegment], step: float = 2.0) -> float | None:
        """Share of frames sampled every `step` seconds across the segments that show a face."""
        hits = []
        for g in segments:
            t = g.start + min(0.5, g.duration / 2)
            while t < g.end:
                r = self.has_face(t)
                if r is not None:
                    hits.append(r)
                t += step
        return round(sum(hits) / len(hits), 3) if hits else None

    def close(self) -> None:
        self._cap.release()
