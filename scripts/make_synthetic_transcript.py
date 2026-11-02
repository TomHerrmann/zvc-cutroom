"""Build the synthetic transcript used by tests and the smoke eval set.

It is invented text with evenly spaced word timings. It exists so the harness can
be tested before real footage is wired in. It is never reported as a real result.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from zvc_clips.schemas import Segment, Transcript, Word  # noqa: E402

LINES = [
    (
        0.0,
        "Welcome back to the channel, if you're new hit follow because every week "
        "we dig into a film.",
    ),
    (8.0, "Tonight we are talking about a movie nobody remembers and everybody should."),
    (40.0, "Here's the thing nobody tells you about practical effects."),
    (45.0, "They age better than anything you can render because the light is real."),
    (52.0, "You can feel the weight of the thing in the room."),
    (60.0, "Follow for part two where we break down the final scene."),
    (120.0, "Honestly the second act drags and I will die on that hill."),
    (127.0, "Cut twenty minutes and this is a masterpiece."),
    (200.0, "The twist is that the narrator was dead the whole time."),
    (210.0, "Anyway that is all for tonight, thanks for watching."),
]
WORD_SECONDS = 0.4


def build() -> Transcript:
    segs = []
    for i, (start, text) in enumerate(LINES):
        toks = text.split()
        words = [
            Word(
                text=t,
                start=round(start + k * WORD_SECONDS, 2),
                end=round(start + (k + 1) * WORD_SECONDS, 2),
            )
            for k, t in enumerate(toks)
        ]
        segs.append(Segment(id=i, start=words[0].start, end=words[-1].end, text=text, words=words))
    return Transcript(
        video_id="synthetic-001", source="fixture", duration=300.0, model=None, segments=segs
    )


if __name__ == "__main__":
    out = Path(sys.argv[1])
    out.write_text(build().model_dump_json(indent=2))
    print(f"wrote {out}")
