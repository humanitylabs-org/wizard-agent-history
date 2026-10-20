"""FTS5 query sanitizing, vendored from Hermes so History accepts the same query
text agents already send to Hermes session_search.

Source: NousResearch/hermes-agent hermes_state_search.py
``SessionSearchMixin._sanitize_fts5_query`` at 818c13be1d (MIT License,
Copyright (c) 2025 Nous Research). History has no CJK LIKE fallback, so ``%``
is always stripped.
"""
import re

MAX_FTS5_QUERY_CHARS = 2_048
_FTS5_SPECIAL_CHARS = '+{}():"^@/#&|~[]<>,;!?$=\\\'%'
_FTS5_SPECIAL_RE = re.compile(f"[{re.escape(_FTS5_SPECIAL_CHARS)}]")
_QUOTED_PHRASE_RE = re.compile(r'"[^"]*"')


def sanitize_fts5_query(query: str) -> str:
    query = query[:MAX_FTS5_QUERY_CHARS]
    held = []

    def hold(m):
        held.append(m.group(0))
        return f"\x00Q{len(held) - 1}\x00"

    s = _QUOTED_PHRASE_RE.sub(hold, query).replace('"', ' ')
    s = _FTS5_SPECIAL_RE.sub(' ', s)
    s = re.sub(r"\*+", "*", s)
    s = re.sub(r"(^|\s)\*", r"\1", s)
    s = re.sub(r"(?i)^(AND|OR|NOT)\b\s*", "", s.strip())
    s = re.sub(r"(?i)\s+(AND|OR|NOT)\s*$", "", s.strip())
    s = re.sub(r"\b(\w+(?:[._-]\w+)+)\b", r'"\1"', s)
    for i, quoted in enumerate(held):
        s = s.replace(f"\x00Q{i}\x00", quoted)
    return s.strip()
