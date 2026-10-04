"""
keywords.py
-----------
Whole-word keyword matching shared by portfolio_digest.py and gazette_scanner.py.

Plain `kw in text` substring checks leak badly on short keywords:
"pli" hits "compliance", "rpo" hits "corporate", "cess" hits "process",
"mine" hits "determine", "itc" hits "switch", "wind" hits "window".
Every keyword here is matched as a whole word instead, with plurals allowed:

  "mine"        -> mine, mines            (not determine / examine)
  "penalty"     -> penalty, penalties
  "renewable"   -> renewable, renewables
  "pledge invo*"-> trailing * = prefix match (invoked, invocation)
"""

import re
from functools import lru_cache

_EDGE = r"(?<![a-z0-9])"
_END = r"(?![a-z0-9])"


def _compile(term):
    term = term.lower().strip()
    if term.endswith("*"):
        return re.compile(_EDGE + re.escape(term[:-1]))
    if term.endswith("y") and term[-2:-1] not in ("a", "e", "o", "u"):
        body = re.escape(term[:-1]) + r"(?:y|ies)"
    else:
        body = re.escape(term) + r"(?:s|es)?"
    return re.compile(_EDGE + body + _END)


@lru_cache(maxsize=None)
def _compiled(terms):
    return tuple(_compile(t) for t in terms)


def matches(text, terms):
    """True if any term occurs in text as a whole word (case-insensitive)."""
    low = " ".join(text.lower().split())
    return any(p.search(low) for p in _compiled(tuple(terms)))
