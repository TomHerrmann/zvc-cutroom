from __future__ import annotations

import re

_TOKEN = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)*")


def normalize_tokens(text: str) -> list[str]:
    """Lowercase, unify apostrophes, drop punctuation. 'Don’t STOP!' -> ['don't', 'stop']."""
    text = text.lower().replace("’", "'").replace("‘", "'")
    return _TOKEN.findall(text)


def contains_sequence(haystack: list[str], needle: list[str]) -> bool:
    if not needle:
        return False
    n = len(needle)
    return any(haystack[i : i + n] == needle for i in range(len(haystack) - n + 1))
