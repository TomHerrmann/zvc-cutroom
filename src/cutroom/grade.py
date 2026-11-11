"""Grade fixes as a 3D LUT for the post-clip node of a Resolve color group.

Resolve's scripting API can't move wheels or qualifiers, but it can set a LUT on a color
group's post-clip node, so a grade fix is built here as a .cube and applied on top of the
hand-graded clip nodes. The timeline works in DaVinci Wide Gamut / Intermediate, so the LUT
works in that log space: a LUT built in display space barely moved anything there.

What it does, in order (all of it learned on 16mm Journey ep1, v9 to v9i):

1. White balance and exposure as small per-channel offsets, faded out below the shadows so
   they never lift the blacks.
2. Saturation in the chroma plane around the luma: an overall gain, a softer gain in the skin
   band (soften the pink, never move its hue), and in the red band no extra gain plus a soft
   knee that compresses only the strongest reds (sunburn hotspots, a red poster).
3. An optional shadow lift, 0 by default: true blacks.

Hue rotation in the skin band is kept as a parameter so old versions rebuild exactly, but the
brand rules reject any preset that uses it: Tom's skin stays pink.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from .config import GradeParams

_E1 = np.array([2.0, -1.0, -1.0]) / math.sqrt(6)  # chroma axis toward red
_E2 = np.array([0.0, 1.0, -1.0]) / math.sqrt(2)  # chroma axis green to blue


def _band(angle: np.ndarray, centre: float, width: float) -> np.ndarray:
    d = (angle - centre + 180.0) % 360.0 - 180.0
    return np.exp(-((d / width) ** 2))


def transform(rgb: np.ndarray, p: GradeParams) -> np.ndarray:
    """Apply the grade to DaVinci Intermediate RGB values, shape (..., 3)."""
    x = np.asarray(rgb, dtype=float)
    # 1. Offsets, gated off the blacks: 0 below wb_lo, full strength 0.12 above it.
    gate = np.clip((x.mean(-1, keepdims=True) - p.wb_lo) / 0.12, 0, 1)
    x = x + gate * (np.array([p.dr, p.dg, p.db]) + p.exp)

    # 2. Chroma plane around the luma.
    y = x.mean(-1, keepdims=True)
    c = x - y
    u, v = c @ _E1, c @ _E2
    angle = np.degrees(np.arctan2(v, u))
    mag = np.hypot(u, v)

    # Skin band, only where there is some colour and some light (not noise in the blacks).
    skin = (
        _band(angle, p.hc, p.hw)
        * np.clip((mag - 0.01) / 0.02, 0, 1)
        * np.clip((y[..., 0] - 0.12) / 0.06, 0, 1)
    )
    angle2 = angle + p.hue * skin

    if p.red_w > 0:
        red = _band(angle, p.red_c, p.red_w)
        knee = np.where(mag > p.knee, p.knee + (mag - p.knee) * p.knee_slope, mag)
        mag_k = mag * (1 - red) + knee * red
        gain = p.gsat * (1 - red) + (p.gsat if p.red_gsat is None else p.red_gsat) * red
    else:
        mag_k, gain = mag, p.gsat
    mag2 = mag_k * (1 - p.skin_sat * skin) * gain

    rad = np.radians(angle2)
    out = y + (mag2 * np.cos(rad))[..., None] * _E1 + (mag2 * np.sin(rad))[..., None] * _E2

    # 3. Shadow lift (0 = true blacks).
    if p.lift:
        out = out + p.lift * np.clip(1 - out / p.lift_to, 0, 1) ** 2
    return out


def write_cube(p: GradeParams, path: Path | str, size: int = 33) -> Path:
    """Write the grade as a .cube 3D LUT (red varies fastest, as Resolve expects)."""
    path = Path(path)
    steps = np.linspace(0.0, 1.0, size)
    b, g, r = np.meshgrid(steps, steps, steps, indexing="ij")
    out = transform(np.stack([r, g, b], -1).reshape(-1, 3), p)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        f.write(f"LUT_3D_SIZE {size}\nDOMAIN_MIN 0 0 0\nDOMAIN_MAX 1 1 1\n")
        for row in out:
            f.write(f"{row[0]:.6f} {row[1]:.6f} {row[2]:.6f}\n")
    return path
