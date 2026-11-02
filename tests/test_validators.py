import pytest
from pydantic import ValidationError

from cutroom.schemas import CTA, ClipProposal, QuoteRef, Word
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


def test_shorts_caption_limit(good_clip, transcript, rules):
    clip = good_clip.model_copy(update={"platform": "shorts", "caption": "x" * 101})
    assert codes(clip, transcript, rules) == ["CAPTION_TOO_LONG"]


def test_mid_word_cut_warns(good_clip, transcript, rules):
    clip = good_clip.model_copy(update={"start": 40.2})  # inside "Here's" (40.0-40.4)
    rep = validate_clip(clip, transcript, rules)
    assert rep.passed and any(i.code == "CUT_MID_WORD" for i in rep.issues)


def test_schema_rejects_unknown_fields_and_bad_words():
    with pytest.raises(ValidationError):
        ClipProposal.model_validate(
            {
                "id": "x",
                "video_id": "v",
                "platform": "reels",
                "start": 0,
                "end": 20,
                "hook": "h",
                "caption": "c",
                "mood": "spicy",
            }
        )
    with pytest.raises(ValidationError):
        Word(text="a", start=2.0, end=1.0)
