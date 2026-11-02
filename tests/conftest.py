import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from make_synthetic_transcript import build  # noqa: E402

from zvc_clips.config import load_rules  # noqa: E402
from zvc_clips.schemas import CTA, ClipProposal, CoverCandidate, QuoteRef  # noqa: E402

SMOKE_SET = ROOT / "evals" / "sets" / "synthetic_smoke"


@pytest.fixture
def rules():
    return load_rules()


@pytest.fixture
def transcript():
    return build()


@pytest.fixture
def good_clip():
    return ClipProposal(
        id="t1",
        video_id="synthetic-001",
        platform="reels",
        start=40.0,
        end=64.6,
        hook="hook",
        caption="A caption.",
        hashtags=["#zerovisioncinema"],
        quotes=[QuoteRef(text="the light is real", start=48.0, end=50.2)],
        cta=CTA(text="Follow", kind="overlay", at=1.0),
        covers=[
            CoverCandidate(
                frame_time=46.0,
                overlay_text="Practical effects age better",
                has_face=True,
                selected=True,
            )
        ],
    )
