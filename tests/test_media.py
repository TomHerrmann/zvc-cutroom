import wave

import numpy as np
import pytest

from cutroom.audio import Level
from cutroom.captions import caption_groups, caption_text, write_ass
from cutroom.schemas import Segment, Transcript, Word


def W(text, a, b):
    return Word(text=text, start=a, end=b)


def test_caption_lines_follow_speech_and_fit():
    words = [
        W("A", 0.0, 0.1), W("post-Roe", 0.1, 0.6), W("psychosexual", 0.6, 1.3),
        W("thriller.", 1.3, 1.7), W("Then", 2.6, 2.8), W("more", 2.8, 3.0),
    ]  # fmt: skip
    groups = [[w.text for w in g] for g in caption_groups(words, max_chars=22, max_words=3)]
    # "A post-Roe psychosexual" is 23 characters: too wide. Break after the sentence and at
    # the long pause.
    assert groups == [["A", "post-Roe"], ["psychosexual", "thriller."], ["Then", "more"]]
    assert caption_text("tension. Now, really?") == "tension Now really?"


def test_ass_highlights_the_spoken_word(rules, tmp_path):
    seg = Segment(id=0, start=0, end=1, text="Big fat check.",
                  words=[W("Big", 0, 0.3), W("fat", 0.3, 0.6), W("check.", 0.6, 1.0)])  # fmt: skip
    t = Transcript(video_id="x", source="fixture", duration=1.0, word_timing="aligned",
                   segments=[seg])  # fmt: skip
    out = write_ass(t, rules, tmp_path / "c.ass").read_text()
    events = [line for line in out.splitlines() if line.startswith("Dialogue")]
    assert len(events) == 3  # one per word, each with that word in the highlight color
    assert "{\\c&H00C68C4A}FAT{\\c&H00F6FDFF}" in events[1]
    assert "Montserrat ExtraBold" in out
    with pytest.raises(ValueError):
        write_ass(t.model_copy(update={"word_timing": "estimated"}), rules, tmp_path / "e.ass")


def test_cuts_snap_to_real_silence(tmp_path):
    # 1s of tone, 0.3s of silence, 1s of tone at 16 kHz
    sr = 16000
    tone = (0.3 * np.sin(np.arange(sr) * 2 * np.pi * 220 / sr) * 32767).astype(np.int16)
    x = np.concatenate([tone, np.zeros(int(0.3 * sr), np.int16), tone])
    p = tmp_path / "a.wav"
    with wave.open(str(p), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(x.tobytes())
    lv = Level.from_wav(p)
    # the aligner says the second word starts at 1.36s (late): cut lands just before 1.30
    t, clean = lv.cut_in(1.36, 0.95)
    assert clean and 1.2 < t < 1.3 and lv.at(t, 0.01) < -55
    t, clean = lv.cut_out(0.95, 1.36)  # first word really ends at 1.0
    assert clean and 1.0 < t < 1.1 and lv.at(t, 0.01) < -55
