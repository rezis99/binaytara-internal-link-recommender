"""Anchor text selection.

Candidates are drawn in tier order (approved guide, H1, title, meta/abstract,
body) and EVERY candidate is scored; the winner is the highest product of tier
score and match-quality multiplier. Early exit in tier order was rejected because
it makes a weak Tier 2 match beat a clean Tier 3 one.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache

from config import selectors as sel
from config import settings

_STOP = {
    "the", "a", "an", "and", "or", "but", "of", "in", "on", "at", "to", "for",
    "with", "by", "from", "as", "is", "are", "was", "were", "be", "been", "it",
    "its", "this", "that", "these", "those", "their", "there", "how", "what",
    "why", "when", "who", "which", "can", "could", "will", "would", "should",
    "has", "have", "had", "do", "does", "did", "you", "your", "we", "our",
}


@lru_cache(maxsize=1)
def synonyms() -> dict[str, list[str]]:
    path = settings.ROOT / "config" / "oncology_synonyms.json"
    try:
        raw = json.loads(path.read_text("utf-8"))
    except Exception:                                          # noqa: BLE001
        return {}
    out: dict[str, list[str]] = {}
    for canonical, variants in raw.items():
        forms = [canonical] + list(variants)
        for f in forms:
            key = f.lower()
            out.setdefault(key, [])
            for g in forms:
                if g.lower() != key and g not in out[key]:
                    out[key].append(g)
    return out


def expand(term: str) -> list[str]:
    return synonyms().get((term or "").lower(), [])


def tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9][a-z0-9\-'\u2019]*", (text or "").lower())


def ngrams(text: str, lo: int | None = None, hi: int | None = None) -> list[str]:
    """Content-word n-grams usable as anchors.

    Ordered SHORTEST first. Long n-grams taken from the middle of a title are
    almost always mid-sentence slices rather than noun phrases, and a 2-to-3
    word topical phrase is both a better anchor and safer against keyword
    cannibalization than a 5-word title fragment.
    """
    lo = lo or settings.ANCHOR_MIN_WORDS
    hi = hi or settings.ANCHOR_MAX_WORDS
    # Only consider the part of a title before a colon or dash: "Prostate Cancer
    # and Obesity: Current Hypotheses" should yield the subject, not the subtitle.
    head = re.split(r"[:\u2013\u2014]|\s-\s", text or "")[0] or (text or "")
    # v5b: include Unicode right-single-quote (\u2019) so "Nepal's" stays as one
    # token instead of splitting into "Nepal" + "s".
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9\-'\u2019]*", head)
    scored = []
    for n in range(lo, hi + 1):
        for i in range(len(words) - n + 1):
            gram = words[i:i + n]
            low = [w.lower() for w in gram]
            if low[0] in _STOP or low[-1] in _STOP:
                continue
            if all(w in _STOP or w in sel.EXTRA_STOPWORDS for w in low):
                continue
            if low[0] in sel.SERP_VERBS or low[0] in sel.FRAGMENT_STARTERS:
                continue
            phrase_low = " ".join(low)
            if phrase_low in sel.GENERIC_ANCHORS:
                continue
            if phrase_low in sel.GEOGRAPHIC_ANCHORS:
                continue
            # A phrase pulled from the middle of a title must still read as a
            # noun phrase; require it to start at the title head or right after
            # a stopword boundary.
            if i > 0 and words[i - 1].lower() not in _STOP:
                continue
            scored.append((i > 0, -n, " ".join(gram)))

    # Head-anchored phrases first, then longest. Without the length preference a
    # 2-gram truncation wins over the full phrase: "head and neck cancer" would
    # be offered as "neck cancer", which is a different disease site.
    scored.sort()
    out = [g for _head, _len, g in scored]
    seen, uniq = set(), []
    for g in out:
        k = g.lower()
        if k not in seen:
            seen.add(k)
            uniq.append(g)
    return uniq


def candidates(page: dict, guide: dict) -> list[tuple[str, int]]:
    """Ordered (anchor, tier) pairs for a target page."""
    out: list[tuple[str, int]] = []
    seen: set[str] = set()

    def add(text: str, tier: int) -> None:
        for g in ngrams(text):
            k = g.lower()
            if k not in seen:
                seen.add(k)
                out.append((g, tier))

    approved = (guide.get(page["url"], {}) or {}).get("approved_anchors", [])
    for a in approved:
        w = len(a.split())
        if settings.ANCHOR_MIN_WORDS <= w <= settings.ANCHOR_MAX_WORDS \
                and a.lower() not in seen:
            seen.add(a.lower())
            out.append((a, 1))

    add(page.get("h1") or "", 2)
    add(page.get("title_clean") or "", 3)
    tier4 = page.get("meta_description") or page.get("abstract") or ""
    add(tier4, 4)
    return out


def _find_span(haystack: str, needle: str) -> tuple[int, int] | None:
    m = re.search(r"\b" + re.escape(needle) + r"\b", haystack, re.I)
    return (m.start(), m.end()) if m else None


def match(anchor: str, text: str) -> tuple[str, tuple[int, int] | None, str]:
    """Return (match_type, span, surface_form_as_it_appears)."""
    span = _find_span(text, anchor)
    if span:
        return "Exact in text", span, text[span[0]:span[1]]

    for alt in expand(anchor):
        span = _find_span(text, alt)
        if span:
            return "Synonym in text", span, text[span[0]:span[1]]

    words = [w for w in tokens(anchor) if w not in _STOP]
    if words:
        low = text.lower()
        positions = []
        for w in words:
            m = re.search(r"\b" + re.escape(w) + r"\b", low)
            if not m:
                positions = []
                break
            positions.append((m.start(), m.end()))
        if positions:
            start, end = min(p[0] for p in positions), max(p[1] for p in positions)
            window_tokens = len(text[start:end].split())
            if window_tokens <= 20:
                return "Partial in text", (start, end), text[start:end]
    return "Needs insertion", None, anchor


def select(page: dict, chunk_text: str, guide: dict) -> dict | None:
    """Best anchor for this target in this text."""
    best = None
    for anchor, tier in candidates(page, guide):
        mtype, span, surface = match(anchor, chunk_text)
        score = settings.ANCHOR_TIER_SCORE[tier] * settings.MATCH_MULTIPLIER[mtype]
        if best is None or score > best["anchor_score"] or (
            score == best["anchor_score"] and tier < best["tier"]
        ):
            best = {
                "anchor": anchor, "tier": tier, "match_type": mtype,
                "span": span, "surface": surface, "anchor_score": score,
            }
    if best and best["match_type"] in ("Exact in text", "Synonym in text"):
        # Display the phrase as it actually appears in the writer's text.
        best["anchor"] = best["surface"]
    return best
