"""The SOP rule engine (R1 to R11) plus scoring.

v3 change: final_score now accepts a keyword_score parameter from the keyword
scanner. The keyword signal is weighted alongside semantic and lexical scores
rather than being a bolt-on floor.
"""
from __future__ import annotations

import re
from datetime import date

from config import selectors as sel
from config import settings
from config import url_rules as ur
from engine import cannibalization, conference


# ---------------------------------------------------------------- R8
def anchor_word_count_ok(anchor: str) -> bool:
    n = len((anchor or "").split())
    return settings.ANCHOR_MIN_WORDS <= n <= settings.ANCHOR_MAX_WORDS


# ---------------------------------------------------------------- R9
def url_ok(url: str) -> bool:
    if not url or not url.startswith("https://"):
        return False
    if "?" in url or "#" in url:
        return False
    return ur.is_allowed(url)


# ---------------------------------------------------------------- R7
def already_linked_in_body(source_page: dict, target_url: str) -> bool:
    return target_url in set(source_page.get("body_internal_links") or [])


# ---------------------------------------------------------------- R10
def contributor_named(target_page: dict, paragraph_text: str) -> tuple[bool, str]:
    if not ur.is_contributor(target_page["url"]):
        return True, ""
    text = paragraph_text or ""
    for variant in target_page.get("person_names") or []:
        if variant.startswith("SURNAME_ONLY::"):
            surname = variant.split("::", 1)[1]
            pattern = (r"\b(?:Dr\.?|Prof\.?|Professor)\s+" + re.escape(surname) +
                       r"\b|\b" + re.escape(surname) +
                       r"\b\s*,?\s*(?:MD|PhD|DO|MPH|RN|DNB|MBBS)\b")
            if re.search(pattern, text, re.I):
                return True, f"Contributor '{surname}' named with a title in this paragraph"
            continue
        if re.search(r"\b" + re.escape(variant) + r"\b", text, re.I):
            return True, f"Contributor '{variant}' named in this paragraph"
    return False, ""


# ---------------------------------------------------------------- R11
def conference_ok(target_page: dict, score: float,
                  today: date | None = None) -> tuple[bool, str, bool]:
    if target_page.get("section") != "Conference":
        return True, "", False
    active, note = conference.is_active(target_page, today)
    if not active:
        return False, note, False
    if score < settings.BAND_MEDIUM:
        return False, note, False
    if score < settings.CONFERENCE_MIN_SCORE:
        return True, (f"{note}. Below the {settings.CONFERENCE_MIN_SCORE} conference "
                      "confidence minimum; verify relevance and dates before linking"), True
    return True, note, False


# ---------------------------------------------------------------- scoring
def keyword_evidence(tier: int, match_type: str, anchor: str) -> float:
    if tier > 2 or match_type not in ("Exact in text", "Synonym in text"):
        return 0.0
    words = [w for w in (anchor or "").lower().split() if w]
    if len(words) < settings.KEYWORD_EVIDENCE_MIN_WORDS:
        return 0.0
    phrase = " ".join(words)
    if phrase in sel.GENERIC_ANCHORS or phrase in sel.GEOGRAPHIC_ANCHORS:
        return 0.0
    return settings.KEYWORD_EVIDENCE_FLOOR


def final_score(semantic: float, lexical: float, anchor_score: float,
                keyword_score: float = 0.0, title_sim: float = 0.0,
                extra_floor: float = 0.0,
                deorphan: bool = False, inbound: int = 0,
                synthetic: bool = False, evidence: float = 0.0) -> float:
    """v3 scoring: four weighted signals plus floors from keyword evidence,
    keyword scan hits, and title similarity.

    keyword_score comes from the keyword scanner (how many of the article's
    disease terms appear in the target page's body text).
    title_sim comes from pairwise H1 Jaccard similarity.
    """
    ws = settings.W_SEMANTIC
    wl = settings.W_LEXICAL
    wk = settings.W_KEYWORD
    total_w = ws + wl + wk

    relevance = (ws * semantic + wl * lexical + wk * keyword_score) / total_w
    # Floors: any of these independent signals can rescue a candidate.
    relevance = max(relevance, evidence)
    if keyword_score >= 0.30:
        relevance = max(relevance, settings.KEYWORD_HIT_FLOOR)
    if title_sim >= settings.TITLE_SIMILARITY_MIN:
        relevance = max(relevance, settings.TITLE_SIMILARITY_FLOOR)
    # v4: awareness-month and conference disease matches carry their own floor.
    if extra_floor > 0:
        relevance = max(relevance, extra_floor)

    s = relevance * (1.0 - settings.W_ANCHOR + settings.W_ANCHOR * anchor_score)
    if synthetic:
        s *= settings.SYNTHETIC_PENALTY
    if deorphan:
        s += settings.DEORPHAN_BONUS * (
            1 - min(inbound, settings.DEORPHAN_CAP) / settings.DEORPHAN_CAP
        )
    return round(min(s, 1.0), 4)


def band(score: float) -> str | None:
    if score >= settings.BAND_HIGH:
        return "High"
    if score >= settings.BAND_MEDIUM:
        return "Medium"
    if score >= settings.BAND_MIN:
        return "Lower"
    return None


def benchmark(word_count: int) -> str:
    for ceiling, label in settings.LINK_BENCHMARK:
        if word_count < ceiling:
            return label
    return settings.LINK_BENCHMARK[-1][1]


# ---------------------------------------------------------------- R5, R6
def enforce_caps(rows: list[dict]) -> list[dict]:
    rows = sorted(rows, key=lambda r: -r["score"])
    best_for_target: dict[str, dict] = {}
    for r in rows:
        prev = best_for_target.get(r["target_url"])
        if prev is None or r["score"] > prev["score"]:
            best_for_target[r["target_url"]] = r
    kept = list(best_for_target.values())

    per_block: dict[int, int] = {}
    out = []
    for r in sorted(kept, key=lambda r: -r["score"]):
        b = r["block_index"]
        if per_block.get(b, 0) >= settings.MAX_PER_PARAGRAPH:
            continue
        per_block[b] = per_block.get(b, 0) + 1
        out.append(r)
    return sorted(out, key=lambda r: (r["block_index"], -r["score"]))


DRAFT_SCHEME = "draft://"
DRAFT_PLACEHOLDER = "this article's URL once it is published"


def display_url(url: str) -> str:
    return DRAFT_PLACEHOLDER if (url or "").startswith(DRAFT_SCHEME) else url


def modified_sentence(text: str, span, anchor: str, url: str, match_type: str,
                      llm_rewrite: str | None = None) -> str:
    """v3: if an LLM rewrite is provided for 'Needs insertion', use it."""
    shown = display_url(url)
    if match_type == "Needs insertion" or not span:
        if llm_rewrite:
            return llm_rewrite
        return (f'Writer to incorporate the phrase "{anchor}" naturally into this '
                f"sentence, then link it to {shown}")
    start, end = span
    return f"{text[:start]}[{text[start:end]}]({shown}){text[end:]}"


def crowding_note(page: dict) -> str:
    n = len(page.get("body_internal_links") or [])
    if n >= settings.CROWDED_PAGE_LINKS:
        return (f"This page already has {n} internal links; only add from a highly "
                "relevant anchor")
    return ""


def overlap(source_page: dict, target_page: dict, anchor: str,
            cmap: dict | None = None) -> tuple[str, str, str]:
    """Keyword competition between two pages. Returns (level, why, basis).

    v4: when a Semrush-derived cannibalization map is available, it is used and
    the basis is reported as "query data". That measures what the label claims:
    whether the two pages rank for the same searches. Without the map the v3
    title heuristic still runs, and the basis says so, so a writer can tell a
    measured signal from an inferred one.
    """
    from engine import cannibalization_data

    if cmap:
        level, why = cannibalization_data.assess(
            source_page.get("url", ""), target_page.get("url", ""), cmap)
        if level:                       # map covered this pair
            # The anchor check still applies: never point an outbound link at
            # another page using the phrase this article ranks for itself.
            a_level, a_why = cannibalization.assess(source_page, target_page, anchor)
            if a_level == "High":
                return "High", a_why, "anchor check"
            return level, why, "query data"

    level, why = cannibalization.assess(source_page, target_page, anchor)
    return level, why, "title heuristic"
