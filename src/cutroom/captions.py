"""Burned-in captions for vertical clips: short lines, the spoken word highlighted.

Resolve's scripting API can't style subtitles or animate them word by word, so the
captions are written as an ASS file and rendered by ffmpeg/libass to a transparent
ProRes 4444 overlay that sits above the video in Resolve. Needs real word times
(`word_timing` exact or aligned); evenly spread SRT times would make the highlight drift.
"""

from __future__ import annotations

import re
from pathlib import Path

from .config import BrandRules
from .schemas import Format, Transcript, Word

_END = re.compile(r"[.?!]$")
_SOFT = re.compile(r"[,;:]$")


def caption_groups(
    words: list[Word], max_chars: int, max_words: int = 4, max_gap: float = 0.45
) -> list[list[Word]]:
    """Split words into on-screen lines that fit the frame and follow the speech:
    break at sentence ends, at long pauses, and before a line gets too wide."""
    groups: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        if cur:
            text = " ".join(x.text for x in cur + [w])
            prev = cur[-1]
            if (
                len(caption_text(text)) > max_chars
                or len(cur) >= max_words
                or _END.search(prev.text)
                or (_SOFT.search(prev.text) and len(cur) >= 2)
                or w.start - prev.end > max_gap
            ):
                groups.append(cur)
                cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    return groups


def caption_text(text: str) -> str:
    """What goes on screen: sentence punctuation dropped, ? and ! kept."""
    return re.sub(r"[.,;:](?=\s|$)", "", text).strip()


def _ass_color(hex_rgb: str) -> str:
    r, g, b = hex_rgb[1:3], hex_rgb[3:5], hex_rgb[5:7]
    return f"&H00{b}{g}{r}".upper()


def _ts(t: float) -> str:
    t = max(t, 0.0)
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def write_ass(
    transcript: Transcript, rules: BrandRules, path: Path, *, end_at: float | None = None
) -> Path:
    """Vertical-format ASS for one clip (transcript already on the clip's timeline)."""
    if transcript.word_timing == "estimated":
        raise ValueError("captions need real word times; run `cutroom align` first")
    sub = rules.subtitles
    spec = rules.formats[Format.VERTICAL]
    style = sub.caption_style
    groups = caption_groups(
        transcript.words(), sub.max_chars_per_line[Format.VERTICAL], style.max_words
    )
    text_c, hi_c = _ass_color(sub.text_color), _ass_color(sub.highlight_color)
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {spec.width}",
        f"PlayResY: {spec.height}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Caption,{style.font},{style.size},{text_c},{text_c},&H00000000,&H64000000,"
        f"0,0,0,0,100,100,0,0,1,{style.outline},{style.shadow},2,{style.margin_x},"
        f"{style.margin_x},{style.margin_bottom},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for gi, g in enumerate(groups):
        nxt = groups[gi + 1][0].start if gi + 1 < len(groups) else None
        g_end = g[-1].end + style.hold
        if nxt is not None:
            g_end = min(g_end, nxt)
        if end_at is not None:
            g_end = min(g_end, end_at)
        shown = [caption_text(w.text) for w in g]
        if style.uppercase:
            shown = [s.upper() for s in shown]
        for i, w in enumerate(g):
            a = g[0].start if i == 0 else w.start
            b = g[i + 1].start if i + 1 < len(g) else g_end
            if b <= a:
                continue
            parts = [
                (f"{{\\c{hi_c}}}{s}{{\\c{text_c}}}" if j == i else s) for j, s in enumerate(shown)
            ]
            lines.append(f"Dialogue: 0,{_ts(a)},{_ts(b)},Caption,,0,0,0,,{' '.join(parts)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
