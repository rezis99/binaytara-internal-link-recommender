"""v7 Orchestrator.

Pipeline, both directions:
  1. Pre-filter candidates (keyword scan + disease matching) -> code, fast
  2. Gemini judges relevance: YES/NO per candidate -> one API call
  3. For each approved candidate: Gemini picks the placement, the anchor,
     and writes the complete modified sentence in one call, validated and
     retried up to 3 times (engine.gemini.generate_link). A candidate that
     never produces a valid result is DROPPED, not shown as a manual
     "Writer to incorporate..." instruction.
  4. Code applies SOP rules that need no judgment: one-anchor-one-target,
     contributor naming, conference expiry, per-paragraph caps.
  5. Excel formatter outputs one merged Give+Receive sheet per article.

Structural fixes in this version, each tied to a specific manual-QA finding:
  - Listing/category/homepage pages excluded as candidates on both sides
    (they matched articles to their own teaser text).
  - Receive-side candidate text is fetched live rather than read from the
    index, because the stored body text was lowercased at build time
    until this version (fixed going forward; this works around the
    existing index without waiting for a reindex).
  - Paragraphs already containing a link, bio-shaped paragraphs, and
    single-sentence statistics lists are excluded from the candidate
    pool before Gemini ever sees them.
"""
from __future__ import annotations

import re
from datetime import datetime

from config import settings
from engine import gemini, input_parser, keyword_scan, link_validation as lv
from engine import retrieval, rules

SECTIONS = ["TCN", "IJCCD", "Blog", "Conference", "Project", "Static", "Contributor"]

# QA #3: pages whose entire content is a list of other articles (the
# homepage, section index, "all articles", category/tag pages). These
# always contain the analysed article's own title in their teaser grid,
# so naive keyword matching "finds" the article on its own listing page.
_LISTING_URL_RE = re.compile(
    r"^https://binaytara\.org/(cancernews(/(all-articles|cancer-education|"
    r"perspectives|clinical-breakthroughs.*|health-equity))?/?)?$"
    r"|^https://binaytara\.org/?$")


def _sections_filter(page: dict, allowed: set[str] | None) -> bool:
    return allowed is None or page.get("section") in allowed


def _is_listing_page(url: str) -> bool:
    return bool(_LISTING_URL_RE.match((url or "").rstrip("/") + "/")) or \
        bool(_LISTING_URL_RE.match(url or ""))


def _is_hub_skip(page: dict) -> bool:
    """Skip 101/overview pages when their cancer type has a planned hub page."""
    if not settings.HUB_SKIP_101_WHEN_PLANNED:
        return False
    config = keyword_scan._hub_pages_config()
    if not config:
        return False
    title_lower = (page.get("h1") or page.get("title_clean") or "").lower()
    url_lower = page.get("url", "").lower()
    for cancer_type, info in config.items():
        if info.get("live"):
            continue
        if cancer_type in title_lower and ("101" in title_lower or "101" in url_lower):
            return True
    return False


# ---------------------------------------------------------------- pre-filter

def _pre_filter_give(article: dict, store: retrieval.Store,
                     allowed_sections: set[str] | None) -> list[dict]:
    body_texts = getattr(store, "body_texts", {}) or {}
    exclude = {article["url"]} | set(article.get("body_internal_links") or [])

    article_terms = keyword_scan.extract_terms(article)
    article_diseases = keyword_scan.article_diseases(article)
    kw_scores = keyword_scan.scan_pages(article_terms, body_texts, exclude)
    title_sims = keyword_scan.title_similarity(article, store.pages, exclude)
    aware = keyword_scan.awareness_and_conference_matches(article, store.pages, exclude)

    candidate_urls = set()
    for url, score in kw_scores.items():
        if score >= 0.10:
            candidate_urls.add(url)
    candidate_urls.update(title_sims)
    candidate_urls.update(aware)

    for url, page in store.pages.items():
        if url in exclude:
            continue
        h1_lower = (page.get("h1") or page.get("title_clean") or "").lower()
        if any(d in h1_lower for d in article_diseases):
            candidate_urls.add(url)

    candidates = []
    for url in candidate_urls:
        if _is_listing_page(url):
            continue
        page = store.page(url)
        if page is None or not _sections_filter(page, allowed_sections):
            continue
        if not rules.url_ok(url) or _is_hub_skip(page):
            continue
        if page.get("section") == "IJCCD":
            tgt_blob = f"{page.get('h1') or ''} {page.get('title_clean') or ''}".lower()
            if not any(d in tgt_blob for d in article_diseases):
                continue
        candidates.append(page)

    candidates.sort(key=lambda p: -kw_scores.get(p["url"], 0.0))
    return candidates[:settings.GEMINI_GIVE_CANDIDATES]


def _pre_filter_receive(article: dict, store: retrieval.Store,
                        allowed_sections: set[str] | None) -> list[dict]:
    body_texts = getattr(store, "body_texts", {}) or {}
    article_terms = keyword_scan.extract_terms(article)

    already_inbound = {
        u for u, p in store.pages.items()
        if article["url"] in set(p.get("body_internal_links") or [])
    }
    recv_exclude = {article["url"]} | already_inbound

    kw_scores = keyword_scan.scan_pages(article_terms, body_texts, recv_exclude)

    source_body = " ".join(
        (b.get("text") if isinstance(b, dict) else getattr(b, "text", ""))
        for b in article.get("blocks", [])
    )
    reverse_scores = keyword_scan.reverse_scan(source_body, store.pages, body_texts, recv_exclude)
    for url, score in reverse_scores.items():
        kw_scores[url] = max(kw_scores.get(url, 0.0), score)

    candidates = []
    for url, _score in sorted(kw_scores.items(), key=lambda x: -x[1]):
        if _is_listing_page(url):
            continue
        page = store.page(url)
        if page is None or not _sections_filter(page, allowed_sections):
            continue
        if page.get("section") in ("IJCCD", "Contributor"):
            continue  # QA B3/#: bios and journal papers are not link sources
        if not rules.url_ok(url):
            continue
        candidates.append(page)

    return candidates[:settings.GEMINI_RECEIVE_CANDIDATES]


# ---------------------------------------------------------------- paragraph ranking

_STOP = {"the", "a", "an", "of", "and", "or", "to", "in", "for", "with",
        "is", "are", "was", "were", "this", "that", "on", "at"}


def _rank_paragraphs(chunks: list[dict], target_h1: str, exclude_first: bool,
                     limit: int) -> list[dict]:
    """Score each eligible chunk against the target's H1 by word overlap,
    drop chunks that can never carry a link, return the top N with their
    original chunk index preserved for Gemini's paragraph_index reference.

    Eligibility (all QA-driven):
      - not the article's first paragraph, when the article side is GIVE
        (SOP R1; not applicable to a receive-side source page)
      - placement_ok (already excludes Key Takeaways / references / quotes,
        via the shared block classifier)
      - does not already contain a link (QA #B9)
      - not a bio-shaped paragraph (QA #9)
      - has at least one sentence that is not a statistics list (QA #10)
    """
    target_words = {w for w in re.findall(r"[a-z]+", (target_h1 or "").lower())
                    if w not in _STOP and len(w) > 3}

    scored = []
    for i, c in enumerate(chunks):
        if exclude_first and i == 0:
            continue
        if not c.get("placement_ok", True):
            continue
        text = c.get("text", "")
        if not text or len(text.split()) < 8:
            continue
        if c.get("existing_links"):
            continue
        if lv.is_bio_paragraph(text):
            continue
        if not lv.find_eligible_sentence(text):
            continue

        words = {w for w in re.findall(r"[a-z]+", text.lower()) if len(w) > 3}
        score = len(words & target_words)
        scored.append((score, c.get("block_index", i), text))

    scored.sort(key=lambda t: -t[0])
    return [{"index": idx, "text": text} for _score, idx, text in scored[:limit]]


# ---------------------------------------------------------------- give

def links_to_give(article: dict, store: retrieval.Store,
                  allowed_sections: set[str] | None = None) -> list[dict]:
    cmap = getattr(store, "cannibalization", {}) or {}
    funnel = article.setdefault("_funnel", {})

    candidates = _pre_filter_give(article, store, allowed_sections)
    funnel["give_prefiltered"] = len(candidates)

    gemini_ok, _ = gemini.is_available()
    if gemini_ok:
        candidates = gemini.judge_give_candidates(article, candidates)
        approved = [c for c in candidates if c.get("gemini_approved", True)]
    else:
        approved = candidates
    funnel["give_gemini_approved"] = len(approved)

    dropped_no_link = 0
    dropped_dup_anchor = 0
    dropped_rules = 0
    used_anchors: dict[str, str] = {}
    rows: list[dict] = []
    chunks = article.get("chunks", [])

    for target in approved:
        paragraphs = _rank_paragraphs(
            chunks, target.get("h1") or target.get("title_clean") or "",
            exclude_first=True, limit=6)
        if not paragraphs:
            dropped_no_link += 1
            continue

        result = gemini.generate_link(paragraphs, target, max_attempts=3)
        if result is None:
            dropped_no_link += 1
            continue

        anchor = result["anchor"]
        anchor_lower = anchor.lower()
        if anchor_lower in used_anchors and used_anchors[anchor_lower] != target["url"]:
            dropped_dup_anchor += 1
            continue
        used_anchors[anchor_lower] = target["url"]

        named_ok, name_note = rules.contributor_named(target, result["existing_sentence"])
        if not named_ok:
            dropped_rules += 1
            continue
        if rules.already_linked_in_body(article, target["url"]):
            dropped_rules += 1
            continue

        score = 0.75
        keep, conf_note, force_lower = rules.conference_ok(target, score)
        if not keep:
            dropped_rules += 1
            continue

        level, why, basis = rules.overlap(article, target, anchor, cmap)
        is_journal = target.get("section") == "IJCCD"

        block_idx = result.get("paragraph_index")
        notes_parts = [
            target.get("gemini_reason", ""),
            name_note, conf_note,
            "IJCCD research paper: consider placing in references" if is_journal else "",
            f"Keyword Competition: {level} ({basis})" if level not in ("", "None") else "",
        ]
        hub_note = keyword_scan.hub_page_note(target.get("h1") or target.get("title_clean") or "")
        if hub_note and not hub_note.startswith("HUB_LIVE:"):
            notes_parts.append(hub_note)

        rows.append({
            "direction": "GIVE",
            "block_index": block_idx if block_idx is not None else 0,
            "existing_sentence": result["existing_sentence"],
            "modified_sentence": result["modified_sentence"],
            "anchor": anchor,
            "target_url": target["url"],
            "target_title": target.get("h1") or target.get("title_clean") or "",
            "source_url": article["url"],
            "source_title": article.get("h1") or article.get("title_clean") or "",
            "section": target.get("section", ""),
            "relevance": "Lower" if force_lower else "High",
            "match_type": "LLM generated",
            "overlap_level": level, "overlap_why": why, "overlap_basis": basis,
            "score": score,
            "notes": ". ".join(n for n in notes_parts if n),
            "review_context": target.get("gemini_reason", ""),
        })

    funnel["give_dropped_no_link"] = dropped_no_link
    funnel["give_dropped_dup_anchor"] = dropped_dup_anchor
    funnel["give_dropped_rules"] = dropped_rules
    funnel["give_before_caps"] = len(rows)
    rows = [r for r in rows]  # enforce_caps below expects block_index key, present
    rows = rules.enforce_caps(rows)
    funnel["give_final"] = len(rows)
    return rows


# ---------------------------------------------------------------- receive

def links_to_receive(article: dict, store: retrieval.Store,
                     allowed_sections: set[str] | None = None) -> list[dict]:
    cmap = getattr(store, "cannibalization", {}) or {}
    funnel = article.setdefault("_funnel", {})

    candidates = _pre_filter_receive(article, store, allowed_sections)
    funnel["recv_prefiltered"] = len(candidates)

    gemini_ok, _ = gemini.is_available()
    if gemini_ok:
        candidates = gemini.judge_receive_candidates(article, candidates)
        approved = [c for c in candidates if c.get("gemini_approved", True)]
    else:
        approved = candidates
    funnel["recv_gemini_approved"] = len(approved)

    dropped_no_link = 0
    dropped_fetch_fail = 0
    rows: list[dict] = []
    target_like = {
        "url": article["url"],
        "h1": article.get("h1") or article.get("title_clean") or "",
        "title_clean": article.get("title_clean", ""),
        "section": "TCN",
    }

    for source in approved:
        try:
            live = input_parser.from_url(source["url"])
        except Exception:
            dropped_fetch_fail += 1
            continue

        paragraphs = _rank_paragraphs(
            live.get("chunks", []), target_like["h1"], exclude_first=False, limit=6)
        if not paragraphs:
            dropped_no_link += 1
            continue

        result = gemini.generate_link(paragraphs, target_like, max_attempts=3)
        if result is None:
            dropped_no_link += 1
            continue

        anchor = result["anchor"]
        level, why, basis = rules.overlap(source, article, anchor, cmap)

        rows.append({
            "direction": "RECEIVE",
            "block_index": result.get("paragraph_index") or 0,
            "existing_sentence": result["existing_sentence"],
            "modified_sentence": result["modified_sentence"],
            "anchor": anchor,
            "target_url": article["url"],
            "target_title": target_like["h1"],
            "source_url": source["url"],
            "source_title": source.get("h1") or source.get("title_clean") or "",
            "section": source.get("section", ""),
            "relevance": "High",
            "match_type": "LLM generated",
            "overlap_level": level, "overlap_why": why, "overlap_basis": basis,
            "score": 0.75,
            "notes": source.get("gemini_reason", ""),
            "review_context": source.get("gemini_reason", ""),
        })

    funnel["recv_dropped_no_link"] = dropped_no_link
    funnel["recv_dropped_fetch_fail"] = dropped_fetch_fail
    funnel["recv_final"] = len(rows)
    rows.sort(key=lambda r: -r["score"])
    return rows


# ---------------------------------------------------------------- top level

def analyse(article: dict, store: retrieval.Store,
            allowed_sections: set[str] | None = None,
            show_lower: bool = True, deorphan: bool = False) -> dict:
    article["_diseases"] = sorted(keyword_scan.article_diseases(article))

    give = links_to_give(article, store, allowed_sections)
    receive = links_to_receive(article, store, allowed_sections)

    gemini_ok, gemini_provider = gemini.is_available()

    return {
        "article": {
            "title": article.get("h1") or article.get("title_clean") or article["url"],
            "url": article["url"],
            "is_draft": article.get("is_draft", False),
            "word_count": article.get("word_count", 0),
            "benchmark": rules.benchmark(article.get("word_count", 0)),
            "existing_links": article.get("body_internal_links") or [],
            "eligible_paragraphs": len([c for c in article.get("chunks", [])
                                        if c.get("placement_ok", True)]),
        },
        "give": give,
        "receive": receive,
        "merged": sorted(give + receive, key=lambda r: -r["score"]),
        "index": {
            "built_at": store.manifest.get("built_at", ""),
            "pages": store.manifest.get("pages", 0),
            "age_days": round(retrieval.index_age_days(store), 1),
        },
        "gemini": {"available": gemini_ok, "provider": gemini_provider},
        "funnel": article.get("_funnel", {}),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
