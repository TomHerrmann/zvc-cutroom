import json
import math

import numpy as np
import pytest
from typer.testing import CliRunner

from cutroom.cli import app
from cutroom.config import GradeParams, GradeRules
from cutroom.grade import _E1, _E2, transform, write_cube


def chroma(rgb):
    x = np.asarray(rgb, dtype=float)
    c = x - x.mean()
    u, v = c @ _E1, c @ _E2
    return math.degrees(math.atan2(v, u)), math.hypot(u, v)


def at(level, angle, mag):
    a = math.radians(angle)
    return level + mag * math.cos(a) * _E1 + mag * math.sin(a) * _E2


def test_shipped_preset_keeps_skin_hue_and_softens_it(rules):
    p = rules.grade.presets[rules.grade.default]
    skin = at(0.45, 15, 0.04)  # Tom's pink, mid-tone
    a0, m0 = chroma(skin)
    a1, m1 = chroma(transform(skin, p.model_copy(update={"dr": 0.0, "db": 0.0})))
    assert a1 == pytest.approx(a0, abs=1e-6)  # the grade itself never rotates skin hue
    assert m1 < m0 * 1.15  # softened against the overall saturation boost
    a2, _ = chroma(transform(skin, p))
    assert abs(a2 - a0) < 3  # the gentle cooling moves it a little, no more


def test_blacks_stay_true(rules):
    p = rules.grade.presets[rules.grade.default]
    for level in (0.0, 0.03, 0.06):
        grey = np.array([level] * 3)
        assert transform(grey, p) == pytest.approx(grey, abs=1e-9)


def test_red_knee_pulls_back_only_hot_reds():
    p = GradeParams(red_c=8, red_w=18, knee=0.075, knee_slope=0.55, red_gsat=1.0, gsat=1.15)
    mild, hot = at(0.4, 8, 0.05), at(0.4, 8, 0.15)
    assert chroma(transform(mild, p))[1] == pytest.approx(0.05, rel=0.01)  # untouched
    hot_out = chroma(transform(hot, p))[1]
    assert hot_out == pytest.approx(0.075 + (0.15 - 0.075) * 0.55, rel=0.01)
    blue = at(0.4, 200, 0.05)  # away from the red band the overall saturation applies
    assert chroma(transform(blue, p))[1] == pytest.approx(0.05 * 1.15, rel=0.01)


def test_brand_rules_refuse_hue_shift_and_lift():
    with pytest.raises(ValueError, match="skin hue"):
        GradeRules(presets={"x": GradeParams(hue=20)})
    with pytest.raises(ValueError, match="blacks"):
        GradeRules(presets={"x": GradeParams(lift=0.02)})
    GradeRules(presets={"x": GradeParams(hue=20)}, allow_skin_hue_shift=True)


def test_cube_and_cli(tmp_path, rules):
    p = rules.grade.presets[rules.grade.default]
    lines = write_cube(p, tmp_path / "a.cube", size=5).read_text().splitlines()
    assert lines[0] == "LUT_3D_SIZE 5" and len(lines) == 3 + 5**3
    assert lines[3] == "0.000000 0.000000 0.000000"  # black maps to black

    out = tmp_path / "b.cube"
    res = CliRunner().invoke(app, ["lut", str(out), "--set", "skin_sat=0.19"])
    assert res.exit_code == 0, res.output
    assert json.loads(out.with_suffix(".json").read_text())["skin_sat"] == 0.19
    res = CliRunner().invoke(app, ["lut", str(out), "--set", "hue=20"])
    assert res.exit_code != 0
