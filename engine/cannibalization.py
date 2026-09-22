"""Topic overlap detection (heuristic).

Deliberately NOT called cannibalization in any user-facing string. Comparing H1
and title n-grams detects topical similarity between two pages. Cannibalization
is two pages competing for the same search QUERY, which can only be established
from query-level data (Google Search Console or Semrush). Using the stronger
label would give writers false confidence in a signal that has not measured what
its name claims.
"""
from __future__ import annotations

import re

from config import selectors as sel
from config import settings
from engine.anchor import _STOP

LABEL = "Topic Overlap (heuristic)"


def keywords(page: dict) -> set[frozenset]:
    """Content-word sets from the fields a page is optimised to rank for."""
    out = set()
    for field in ("h1", "title_clean", "meta_description"):
        text = page.get(field) or ""
        words = [w for w in re.findall(r"[a-z0-9]+", text.lower())
                 if w not in _STOP and w not in sel.EXTRA_STOPWORDS and len(w) > 2]
        if words:
            out.add(frozenset(words))
    return out


def _jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def assess(source: dict, target: dict, anchor: str) -> tuple[str, str]:
    """Return (level, explanation). Level is None / Medium / High."""
    src_kw, tgt_kw = keywords(source), keywords(target)

    # Must use the SAME filter as keywords() above. Using a different stopword
    # set here meant a word stripped from the page keywords but kept in the
    # anchor made the subset test fail, silently downgrading a real High flag.
    anchor_words = frozenset(
        w for w in re.findall(r"[a-z0-9]+", (anchor or "").lower())
        if w not in _STOP and w not in sel.EXTRA_STOPWORDS and len(w) > 2
    )
    # The cheapest real protection: never point an outbound link at another page
    # using the phrase this article is itself trying to rank for.
    for s in src_kw:
        if anchor_words and anchor_words.issubset(s):
            return "High", "Anchor matches this article's own H1 or title keywords"

    best = max((_jaccard(s, t) for s in src_kw for t in tgt_kw), default=0.0)
    if best >= settings.OVERLAP_HIGH:
        return "High", f"Both pages target very similar keywords (overlap {best:.2f})"
    if best >= settings.OVERLAP_MEDIUM:
        return "Medium", f"Some keyword overlap between the pages (overlap {best:.2f})"
    return "None", ""
