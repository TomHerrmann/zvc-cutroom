import pytest
from pydantic import ValidationError

from cutroom.schemas import CTA, ClipProposal, ClipSegment, QuoteRef, Word
from cutroom.validators import validate_clip
from cutroom.validators.text import normalize_tokens


def codes(clip, transcript, rules, spoilers=()):
    return validate_clip(clip, transcript, rules, list(spoilers)).error_codes


def test_good_clip_passes(good_clip, transcript, rules):
    rep = validate_clip(good_clip, transcript, rules)
    assert rep.passed, rep.issues


def test_normalization_handles_curly_quotes_and_punctuation():
    assert normalize_tokens("Here’s the THING, nobody...") == ["here's", "the", "thing", "nobody"]


def test_quote_casing_and_punctuation_ignored(good_clip, transcript, rules):
    clip = good_clip.model_copy(
        update={"quotes": [QuoteRef(text="THE LIGHT. IS REAL!", start=48.0, end=50.2)]}
    )
    assert codes(clip, transcript, rules) == []


@pytest.mark.parametrize(
    "text",
    [
        "the light is fake",  # one word changed
        "the light is really real",  # word inserted
        "light real",  # words dropped
    ],
)
def test_near_miss_quotes_are_rejected(good_clip, transcript, rules, text):
    clip = good_clip.model_copy(update={"quotes": [QuoteRef(text=text, start=48.0, end=50.2)]})
    assert "QUOTE_NOT_IN_TRANSCRIPT" in codes(clip, transcript, rules)


def test_real_quote_at_wrong_time_is_rejected(good_clip, transcript, rules):
    clip = good_clip.model_copy(
        update={"quotes": [QuoteRef(text="the light is real", start=120.0, end=122.0)]}
    )
    assert "QUOTE_NOT_IN_TRANSCRIPT" in codes(clip, transcript, rules)


def test_quote_outside_cut_is_warning_only(good_clip, transcript, rules):
    clip = good_clip.model_copy(
        update={
            "quotes": [
                QuoteRef(
                    text="Cut twenty minutes and this is a masterpiece.", start=127.0, end=130.2
                )
            ]
        }
    )
    rep = validate_clip(clip, transcript, rules)
    assert rep.passed
    assert any(i.code == "QUOTE_OUTSIDE_CLIP" for i in rep.issues)


def test_cta_boundary(good_clip, transcript, rules):
    at_limit = good_clip.model_copy(update={"cta": CTA(text="Follow", kind="overlay", at=5.0)})
    late = good_clip.model_copy(update={"cta": CTA(text="Follow", kind="overlay", at=5.01)})
    assert codes(at_limit, transcript, rules) == []
    assert codes(late, transcript, rules) == ["CTA_TOO_LATE"]


def test_cover_unverified_face_fails(good_clip, transcript, rules):
    cover = good_clip.covers[0].model_copy(update={"has_face": None})
    clip = good_clip.model_copy(update={"covers": [cover]})
    assert codes(clip, transcript, rules) == ["COVER_NO_FACE"]


def test_spoiler_matched_across_fields(good_clip, transcript, rules):
    clip = good_clip.model_copy(update={"title_variants": ["The Narrator WAS dead?!"]})
    assert codes(clip, transcript, rules, spoilers=["narrator was dead"]) == ["SPOILER"]


def test_spoiler_does_not_match_substrings(good_clip, transcript, rules):
    clip = good_clip.model_copy(update={"caption": "Deadline cinema."})
    assert codes(clip, transcript, rules, spoilers=["dead"]) == []


def test_bad_hashtag(good_clip, transcript, rules):
    clip = good_clip.model_copy(update={"hashtags": ["#zerovisioncinema", "#two words"]})
    assert codes(clip, transcript, rules) == ["BAD_HASHTAG"]


def test_caption_limit(good_clip, transcript, rules):
    clip = good_clip.model_copy(update={"caption": "x" * 2201})
    assert codes(clip, transcript, rules) == ["CAPTION_TOO_LONG"]


def test_vertical_must_be_1080p(good_clip, transcript, rules):
    clip = good_clip.model_copy(update={"width": 2160, "height": 3840})
    assert codes(clip, transcript, rules) == ["BAD_RESOLUTION"]


def test_brand_subtitle_colors_loaded(rules):
    assert rules.subtitles.text_color == "#FFFDF6"
    assert rules.subtitles.highlight_color == "#4A8CC6"
    assert (rules.formats["wide"].width, rules.formats["wide"].height) == (3840, 2160)
    assert rules.formats["wide"].captions == "srt"
    assert rules.formats["vertical"].captions == "burned_in"


def test_cue_level_quote_matching_for_srt_transcripts(good_clip, transcript, rules):
    # Resolve SRT has no word timing: a quote anywhere in an overlapping cue counts,
    # but it must still be verbatim.
    est = transcript.model_copy(update={"word_timing": "estimated"})
    ok = good_clip.model_copy(
        update={"quotes": [QuoteRef(text="the light is real", start=45.0, end=46.0)]}
    )
    bad = good_clip.model_copy(
        update={"quotes": [QuoteRef(text="the light is fake", start=45.0, end=46.0)]}
    )
    assert codes(ok, est, rules) == []
    assert "QUOTE_NOT_IN_TRANSCRIPT" in codes(bad, est, rules)


def test_cut_through_a_line_is_rejected(good_clip, transcript, rules):
    # starts inside "Here's" (40.0-40.4), so the first line is clipped
    clip = good_clip.model_copy(update={"segments": [ClipSegment(start=40.2, end=64.6)]})
    rep = validate_clip(clip, transcript, rules)
    assert rep.error_codes == ["PARTIAL_LINE"]
    assert any(i.code == "CUT_MID_WORD" for i in rep.issues)


def test_schema_rejects_unknown_fields_and_bad_words():
    with pytest.raises(ValidationError):
        ClipProposal.model_validate(
            {
                "id": "x",
                "video_id": "v",
                "segments": [{"start": 0, "end": 20}],
                "hook": "h",
                "caption": "c",
                "mood": "spicy",
            }
        )
    with pytest.raises(ValidationError):
        Word(text="a", start=2.0, end=1.0)


def test_stitched_clip_from_several_parts(good_clip, transcript, rules):
    clip = good_clip.model_copy(
        update={
            "segments": [ClipSegment(start=40.0, end=50.4), ClipSegment(start=127.0, end=130.4)],
            "quotes": [
                QuoteRef(text="the light is real", start=48.0, end=50.2),
                QuoteRef(
                    text="Cut twenty minutes and this is a masterpiece.", start=127.0, end=130.2
                ),
            ],
        }
    )
    rep = validate_clip(clip, transcript, rules)
    assert abs(clip.duration - 13.8) < 1e-9
    assert clip.source_time(11.0) == 127.6
    assert rep.error_codes == ["BAD_LENGTH"]  # 13.8s is under the 15s minimum
    assert not any(i.code == "QUOTE_OUTSIDE_CLIP" for i in rep.issues)


def test_overlapping_segments_rejected(good_clip, transcript, rules):
    clip = good_clip.model_copy(
        update={"segments": [ClipSegment(start=40.0, end=55.0), ClipSegment(start=50.0, end=64.0)]}
    )
    assert "OVERLAPPING_SEGMENTS" in codes(clip, transcript, rules)


def test_audio_map_edges_and_youtube_only(good_clip, transcript, rules):
    from cutroom.schemas import VideoContext

    video = VideoContext(
        video_id="synthetic-001",
        silences=[(39.8, 40.05), (64.4, 64.8)],
        youtube_only=[(50.0, 52.0)],
    )
    rep = validate_clip(good_clip, transcript, rules, video=video)
    assert rep.error_codes == ["YOUTUBE_ONLY_CONTENT"]
    late = good_clip.model_copy(update={"segments": [ClipSegment(start=40.0, end=62.0)]})
    assert "CUT_MID_SPEECH" in validate_clip(late, transcript, rules, video=video).error_codes
    assert video.snap(64.3) == 64.6


def test_end_card_defaults_loaded(rules):
    ec = rules.end_card
    assert ec.title == "ZVC Topic Label Slam"
    assert ec.topic_title == "FULL VIDEO ON YOUTUBE"
    assert (ec.seconds, ec.transition_in, ec.only_when_long_review) == (3.0, "wipe", True)


def test_end_card_replaces_spoken_outro(good_clip, transcript, rules):
    from cutroom.schemas import VideoContext

    # last line of the clip becomes a spoken sign-off
    segs = [s.model_copy() for s in transcript.segments]
    signoff = next(s for s in segs if s.start == 60.0)
    signoff.text = "Let me know what you think."
    t = transcript.model_copy(update={"segments": segs})
    with_review = VideoContext(video_id="synthetic-001", long_review="https://youtu.be/x")
    no_review = VideoContext(video_id="synthetic-001")

    # longer review exists: needs the card and must cut the sign-off
    assert validate_clip(good_clip, t, rules, video=with_review).error_codes == [
        "MISSING_END_CARD",
        "SPOKEN_OUTRO",
    ]
    fixed = good_clip.model_copy(
        update={
            "segments": [ClipSegment(start=40.0, end=56.6)],
            "end_card": "FULL VIDEO ON YOUTUBE",
        }
    )
    assert validate_clip(fixed, t, rules, video=with_review).error_codes == []
    assert abs(fixed.duration - 16.6) < 1e-9  # the 3s card counts toward the length limits
    # no longer review: the card would point nowhere
    assert validate_clip(fixed, t, rules, video=no_review).error_codes == [
        "END_CARD_WITHOUT_LONG_REVIEW"
    ]
    # no audio map / video context at all: only the card claim is checked
    assert validate_clip(good_clip, t, rules).error_codes == []


def test_estimated_line_times_trust_the_audio_pause(good_clip, transcript, rules):
    from cutroom.schemas import VideoContext

    est = transcript.model_copy(update={"word_timing": "estimated"})
    clip = good_clip.model_copy(update={"segments": [ClipSegment(start=40.2, end=64.6)]})
    video = VideoContext(video_id="synthetic-001", silences=[(40.1, 40.3), (64.4, 64.8)])
    # SRT says the line starts at 40.0, but the audio is silent until 40.3: no words lost
    assert validate_clip(clip, est, rules, video=video).error_codes == []
    # without the audio map there is no proof, so the cut is rejected
    assert "PARTIAL_LINE" in validate_clip(clip, est, rules).error_codes
    # a 0.5s miss is more than estimate slop, even in a pause
    far = good_clip.model_copy(update={"segments": [ClipSegment(start=40.5, end=64.6)]})
    wide = VideoContext(video_id="synthetic-001", silences=[(40.0, 40.6), (64.4, 64.8)])
    assert "PARTIAL_LINE" in validate_clip(far, est, rules, video=wide).error_codes
