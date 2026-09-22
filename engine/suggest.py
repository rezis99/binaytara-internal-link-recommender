"""v6 Orchestrator: Gemini judges, code formats.

Pipeline:
  1. Pre-filter candidates (keyword scan + disease matching) → ~35 pages
  2. Gemini judges: YES/NO per candidate (one API call)
  3. Code selects anchor from destination H1 (string matching, guaranteed correct)
  4. Code finds anchor in source text (exact search)
  5. Code applies SOP rules (R1-R11)
  6. Code builds the Modified Sentence (string replacement)
  7. Excel formatter outputs the file

Gemini does ONE thing: relevance judgment. Everything else is deterministic.
"""
from __future__ import annotations

import re
from datetime import datetime

from config import settings
from config import url_rules as ur
from engine import anchor as anchor_mod
from engine import cannibalization_data, gemini, keyword_scan, retrieval, rules
from indexer import chunker

SECTIONS = ["TCN", "IJCCD", "Blog", "Conference", "Project", "Static", "Contributor"]


def _sections_filter(page: dict, allowed: set[str] | None) -> bool:
    return allowed is None or page.get("section") in allowed


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
            continue  # Hub is live; don't skip, we WANT to link to the hub
        # Skip 101 pages for this cancer type
        if cancer_type in title_lower and ("101" in title_lower or "101" in url_lower):
            return True
    return False


def _pre_filter_give(article: dict, store: retrieval.Store,
                     allowed_sections: set[str] | None) -> list[dict]:
    """Narrow 1,680 pages to ~35 candidates using keyword and disease matching.
    This is the fast, code-based step before Gemini judges."""
    body_texts = getattr(store, "body_texts", {}) or {}
    exclude = {article["url"]} | set(article.get("body_internal_links") or [])

    # Extract disease terms from the article
    article_terms = keyword_scan.extract_terms(article)
    article_diseases = keyword_scan.article_diseases(article)
    kw_scores = keyword_scan.scan_pages(article_terms, body_texts, exclude)
    title_sims = keyword_scan.title_similarity(article, store.pages, exclude)
    aware = keyword_scan.awareness_and_conference_matches(
        article, store.pages, exclude)

    # Collect candidates from all signals
    candidate_urls = set()
    for url, score in kw_scores.items():
        if score >= 0.10:
            candidate_urls.add(url)
    for url in title_sims:
        candidate_urls.add(url)
    for url in aware:
        candidate_urls.add(url)

    # Also add pages whose H1 mentions any of the article's disease terms
    for url, page in store.pages.items():
        if url in exclude:
            continue
        h1_lower = (page.get("h1") or page.get("title_clean") or "").lower()
        for disease in article_diseases:
            if disease in h1_lower:
                candidate_urls.add(url)
                break

    # Filter and limit
    candidates = []
    for url in candidate_urls:
        page = store.page(url)
        if page is None:
            continue
        if not _sections_filter(page, allowed_sections):
            continue
        if not rules.url_ok(url):
            continue
        if _is_hub_skip(page):
            continue
        # IJCCD on give-side: only when disease matches
        if page.get("section") == "IJCCD":
            tgt_blob = f"{page.get('h1') or ''} {page.get('title_clean') or ''}".lower()
            if not any(d in tgt_blob for d in article_diseases):
                continue
        candidates.append(page)

    # Sort by keyword score (best matches first) and limit
    candidates.sort(key=lambda p: -kw_scores.get(p["url"], 0.0))
    return candidates[:settings.GEMINI_GIVE_CANDIDATES]


def _pre_filter_receive(article: dict, store: retrieval.Store,
                        allowed_sections: set[str] | None) -> list[dict]:
    """Narrow to pages that should link TO this article."""
    body_texts = getattr(store, "body_texts", {}) or {}
    article_terms = keyword_scan.extract_terms(article)

    # Pages that already link to this article (exclude them)
    already_inbound = {
        u for u, p in store.pages.items()
        if article["url"] in set(p.get("body_internal_links") or [])
    }
    recv_exclude = {article["url"]} | already_inbound

    # Forward scan: other pages mentioning this article's terms
    kw_scores = keyword_scan.scan_pages(article_terms, body_texts, recv_exclude)

    # Reverse scan: pages whose own disease terms appear in this article
    source_body = " ".join(
        (b.get("text") if isinstance(b, dict) else getattr(b, "text", ""))
        for b in article.get("blocks", [])
    )
    reverse_scores = keyword_scan.reverse_scan(
        source_body, store.pages, body_texts, recv_exclude)
    for url, score in reverse_scores.items():
        kw_scores[url] = max(kw_scores.get(url, 0.0), score)

    candidates = []
    for url, score in sorted(kw_scores.items(), key=lambda x: -x[1]):
        page = store.page(url)
        if page is None:
            continue
        if not _sections_filter(page, allowed_sections):
            continue
        # IJCCD excluded from receive entirely
        if page.get("section") == "IJCCD":
            continue
        if not rules.url_ok(url):
            continue
        candidates.append(page)

    return candidates[:settings.GEMINI_RECEIVE_CANDIDATES]


def _select_anchor_and_sentence(article: dict, target: dict,
                                guide: dict) -> dict | None:
    """Select anchor from the DESTINATION page's H1/title.
    Then find whether that anchor appears in any eligible paragraph.
    If not, pick the BEST paragraph for a Gemini rewrite.

    Returns dict with anchor, sentence, block_index, match_type, span, or None.
    """
    best_result = None
    best_insertion = None  # best candidate for Gemini rewrite
    placeable = [c for c in article.get("chunks", []) if c.get("placement_ok", True)]

    for chunk in placeable:
        result = anchor_mod.select(target, chunk["text"], guide)
        if result is None:
            continue
        if not rules.anchor_word_count_ok(result["anchor"]):
            continue

        if result["match_type"] in ("Exact in text", "Synonym in text"):
            # Best case: anchor already in the text
            if best_result is None or \
                    (result["match_type"] == "Exact in text" and
                     best_result.get("match_type") != "Exact in text"):
                best_result = {**result, "chunk": chunk}
        elif result["match_type"] == "Needs insertion":
            # Track the best "Needs insertion" candidate for Gemini rewrite
            if best_insertion is None:
                best_insertion = {**result, "chunk": chunk}

    # If we have an exact/synonym match, use it (code handles the link wrapping)
    if best_result is not None:
        return best_result

    # If no exact match but we have a "Needs insertion" candidate,
    # try Gemini rewrite
    if best_insertion is not None:
        anchor = best_insertion["anchor"]
        sentence = best_insertion["chunk"]["text"]
        target_title = target.get("h1") or target.get("title_clean") or ""
        target_url = target["url"]

        rewrite = gemini.rewrite_sentence(sentence, anchor, target_title, target_url)
        if rewrite:
            best_insertion["match_type"] = "Gemini rewrite"
            best_insertion["modified_override"] = rewrite
            return best_insertion
        else:
            # Gemini unavailable or rewrite failed validation.
            # Still return as "Needs insertion" so it appears in the output
            # with the manual instruction, rather than being silently dropped.
            return best_insertion

    return None


def _receive_anchor_for_article(article: dict, source_body: str) -> dict | None:
    """For the receive side, find an anchor in the SOURCE page's body that
    describes the DESTINATION article's topic.

    The anchor must describe what the reader will find: not just 'alcohol'
    but 'alcohol and cancer' or 'alcohol cancer risk'.
    """
    h1 = article.get("h1") or article.get("title_clean") or ""
    # Generate anchor candidates from the article's own H1
    candidates = anchor_mod.ngrams(h1)
    source_lower = source_body.lower()

    for cand in candidates:
        cand_lower = cand.lower()
        # Skip generic anchors
        if cand_lower in {"cancer risk", "risk factors", "cancer", "how much"}:
            continue
        # Check if this anchor appears in the source page's body
        if cand_lower in source_lower:
            # Find the exact sentence
            for sent in re.split(r'(?<=[.!?])\s+', source_body):
                if cand_lower in sent.lower():
                    return {
                        "anchor": cand,
                        "sentence": sent.strip(),
                        "match_type": "Exact in text",
                    }

    # Fallback: check for partial matches of the article's disease terms
    diseases = keyword_scan.article_diseases(article)
    for disease in diseases:
        # Look for phrases like "alcohol and [disease]" or "[disease] risk"
        patterns = [
            f"alcohol and {disease}", f"alcohol-related {disease}",
            f"{disease} risk", f"risk of {disease}",
            f"alcohol consumption",  # last resort
        ]
        for pat in patterns:
            if pat.lower() in source_lower:
                for sent in re.split(r'(?<=[.!?])\s+', source_body):
                    if pat.lower() in sent.lower():
                        return {
                            "anchor": pat,
                            "sentence": sent.strip(),
                            "match_type": "Exact in text" if pat != "alcohol consumption"
                                          else "Needs insertion",
                        }

    return None


def links_to_give(article: dict, store: retrieval.Store,
                  allowed_sections: set[str] | None = None) -> list[dict]:
    """Where in this article to place links out to existing pages.

    v6 pipeline: pre-filter → Gemini judges → code selects anchors → format.
    """
    cmap = getattr(store, "cannibalization", {}) or {}

    # Step 1: Pre-filter candidates
    candidates = _pre_filter_give(article, store, allowed_sections)

    # Step 2: Gemini judges relevance
    gemini_ok, _ = gemini.is_available()
    if gemini_ok:
        candidates = gemini.judge_give_candidates(article, candidates)
        approved = [c for c in candidates if c.get("gemini_approved", True)]
    else:
        # Fallback: use all pre-filtered candidates (less precise)
        approved = candidates

    # Step 3: For each approved page, select anchor and find sentence
    used_anchors: dict[str, str] = {}  # anchor_lower → target_url (one-anchor-one-target)
    rows: list[dict] = []

    for target in approved:
        result = _select_anchor_and_sentence(article, target, store.guide)
        if result is None:
            continue

        anchor = result["anchor"]
        anchor_lower = anchor.lower()
        chunk = result["chunk"]

        # One anchor one target: if this anchor already points to another URL, skip
        if anchor_lower in used_anchors and used_anchors[anchor_lower] != target["url"]:
            continue
        used_anchors[anchor_lower] = target["url"]

        # SOP rules
        named_ok, name_note = rules.contributor_named(target, chunk["text"])
        if not named_ok:
            continue
        if rules.already_linked_in_body(article, target["url"]):
            continue

        # Build the modified sentence
        if result.get("modified_override"):
            # Gemini already rewrote the sentence with [anchor](url) in place
            modified = result["modified_override"]
        else:
            modified = rules.modified_sentence(
                chunk["text"], result.get("span"), anchor,
                target["url"], result["match_type"])

        # Scoring (simplified for v6: Gemini already judged relevance)
        score = 0.70 if result["match_type"] in ("Exact in text", "Synonym in text") else 0.50
        b = rules.band(score)
        if b is None:
            continue

        # Cannibalization check
        level, why, basis = rules.overlap(article, target, anchor, cmap)

        # Conference check
        keep, conf_note, force_lower = rules.conference_ok(target, score)
        if not keep:
            continue

        gemini_reason = target.get("gemini_reason", "")
        is_journal = target.get("section") == "IJCCD"
        journal_note = ("IJCCD research paper: consider placing in references"
                        if is_journal else "")

        notes_parts = [
            gemini_reason,
            name_note,
            conf_note,
            journal_note,
            f"Keyword Competition: {level} ({basis})" if level not in ("", "None") else "",
        ]

        existing_links = chunk.get("existing_links", [])
        if existing_links:
            link_urls = [u for u, _a in existing_links]
            notes_parts.append(
                f"Paragraph already has {len(link_urls)} link(s): {', '.join(link_urls[:2])}")

        hub_note = keyword_scan.hub_page_note(
            target.get("h1") or target.get("title_clean") or "")
        if hub_note and not hub_note.startswith("HUB_LIVE:"):
            notes_parts.append(hub_note)

        rows.append({
            "block_index": chunk.get("block_index", 0),
            "existing_sentence": chunk["text"],
            "modified_sentence": modified,
            "anchor": anchor,
            "target_url": target["url"],
            "target_title": target.get("h1") or target.get("title_clean") or "",
            "section": target.get("section", ""),
            "relevance": "Lower" if force_lower else b,
            "match_type": result["match_type"],
            "overlap_level": level,
            "overlap_why": why,
            "overlap_basis": basis,
            "score": score,
            "notes": ". ".join(n for n in notes_parts if n),
            "review_context": gemini_reason,
            "is_topical": True,
            "is_journal_target": is_journal,
        })

    rows = rules.enforce_caps(rows)
    return rows


def links_to_receive(article: dict, store: retrieval.Store,
                     allowed_sections: set[str] | None = None) -> list[dict]:
    """Which existing pages should add a link pointing to this article.

    v6: pre-filter by keyword scan → Gemini judges → code finds anchor.
    """
    body_texts = getattr(store, "body_texts", {}) or {}
    cmap = getattr(store, "cannibalization", {}) or {}

    # Step 1: Pre-filter
    candidates = _pre_filter_receive(article, store, allowed_sections)

    # Step 2: Gemini judges
    gemini_ok, _ = gemini.is_available()
    if gemini_ok:
        candidates = gemini.judge_receive_candidates(article, candidates)
        approved = [c for c in candidates if c.get("gemini_approved", True)]
    else:
        approved = candidates

    # Step 3: For each approved page, find an anchor in its body text
    rows: list[dict] = []
    for source in approved:
        source_body = body_texts.get(source["url"], "")
        if not source_body:
            continue

        result = _receive_anchor_for_article(article, source_body)
        if result is None:
            # No natural anchor found. Try Gemini rewrite on the first
            # sentence that mentions alcohol or the article's disease terms.
            diseases = keyword_scan.article_diseases(article)
            h1 = article.get("h1") or article.get("title_clean") or ""
            # Find a sentence that at least mentions one disease term
            best_sent = None
            for sent in re.split(r'(?<=[.!?])\s+', source_body):
                sent_lower = sent.lower()
                if any(d in sent_lower for d in diseases) or "alcohol" in sent_lower:
                    best_sent = sent.strip()
                    break
            if best_sent is None:
                continue
            # Try a Gemini rewrite
            # Use a short anchor derived from the article's H1
            short_anchors = anchor_mod.ngrams(h1, 2, 4)
            anchor_to_try = short_anchors[0] if short_anchors else h1[:30]
            rewrite = gemini.rewrite_sentence(
                best_sent, anchor_to_try, h1, article["url"])
            if rewrite:
                result = {
                    "anchor": anchor_to_try,
                    "sentence": best_sent,
                    "match_type": "Gemini rewrite",
                    "modified_override": rewrite,
                }
            else:
                continue

        level, why, basis = rules.overlap(source, article, result["anchor"], cmap)

        gemini_reason = source.get("gemini_reason", "")

        # Build modified sentence
        if result.get("modified_override"):
            recv_modified = result["modified_override"]
        else:
            recv_modified = rules.modified_sentence(
                result["sentence"], None, result["anchor"],
                article["url"], result["match_type"])

        rows.append({
            "source_url": source["url"],
            "source_title": source.get("h1") or source.get("title_clean") or "",
            "section": source.get("section", ""),
            "existing_sentence": result["sentence"],
            "modified_sentence": recv_modified,
            "anchor": result["anchor"],
            "relevance": "High" if result["match_type"] == "Exact in text" else "Medium",
            "match_type": result["match_type"],
            "overlap_level": level,
            "overlap_why": why,
            "overlap_basis": basis,
            "score": 0.70 if result["match_type"] == "Exact in text" else 0.50,
            "notes": gemini_reason,
            "review_context": gemini_reason,
            "is_topical": True,
        })

    rows.sort(key=lambda r: -r["score"])
    return rows


def analyse(article: dict, store: retrieval.Store,
            allowed_sections: set[str] | None = None,
            show_lower: bool = True, deorphan: bool = False) -> dict:

    # Enrich article with disease terms for Gemini
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
        "index": {
            "built_at": store.manifest.get("built_at", ""),
            "pages": store.manifest.get("pages", 0),
            "age_days": round(retrieval.index_age_days(store), 1),
        },
        "gemini": {"available": gemini_ok, "provider": gemini_provider},
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
