# Eval: synthetic_smoke (SYNTHETIC fixture, not real ZVC data)

Run 2026-11-02 19:20 UTC, commit `40d405d`, brand rules `37aa9f4c35db`.

| metric | value |
|---|---|
| cases | 17 (3 good, 14 bad) |
| gate accuracy | 100% |
| good clips passed | 100% |
| bad clips caught | 100% |
| caught for the right reason | 100% |

| case | label | gate | ok | codes |
|---|---|---|---|---|
| bad-cover-no-face | bad | reject | yes | COVER_NO_FACE |
| bad-cover-wordy | bad | reject | yes | COVER_WORD_COUNT |
| bad-cta-late | bad | reject | yes | CTA_TOO_LATE |
| bad-fake-spoken-cta | bad | reject | yes | CTA_NOT_SPOKEN |
| bad-horizontal-1080p | bad | reject | yes | BAD_RESOLUTION |
| bad-invented-quote | bad | reject | yes | QUOTE_NOT_IN_TRANSCRIPT |
| bad-missing-cta | bad | reject | yes | MISSING_CTA |
| bad-no-brand-tag | bad | reject | yes | MISSING_REQUIRED_HASHTAG |
| bad-past-end | bad | reject | yes | BAD_TIMESTAMP |
| bad-quote-wrong-time | bad | reject | yes | QUOTE_NOT_IN_TRANSCRIPT |
| bad-short-as-wide | bad | reject | yes | BAD_LENGTH, SHORT_MUST_BE_VERTICAL |
| bad-spoiler | bad | reject | yes | SPOILER |
| bad-srt-on-vertical | bad | reject | yes | BAD_CAPTION_MODE |
| bad-too-long | bad | reject | yes | BAD_LENGTH, PARTIAL_LINE |
| good-01 | good | pass | yes | - |
| good-02 | good | pass | yes | - |
| good-03 | good | pass | yes | - |
