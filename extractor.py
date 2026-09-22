"""HTML to structured page record.

v3 change: returns _body_text (the concatenated body prose) so the indexer can
store it for the keyword scanner without a second crawl pass.
"""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from config import selectors as sel
from config import settings
from config import url_rules as ur
from indexer.blocks import Block, classify


def _text(el) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()


def _link_text_ratio(el) -> float:
    total = len(_text(el))
    if total == 0:
        return 1.0
    linked = sum(len(_text(a)) for a in el.find_all("a"))
    return linked / total


def pick_body(soup: BeautifulSoup):
    for css in sel.BODY_SELECTORS:
        for el in soup.select(css):
            clone = BeautifulSoup(str(el), "lxml")
            strip_junk(clone)
            txt = _text(clone)
            if len(txt) >= settings.MIN_BODY_CHARS and \
                    _link_text_ratio(clone) < settings.MAX_LINK_TEXT_RATIO:
                return clone
    return None


def strip_junk(soup) -> None:
    for css in sel.JUNK_SELECTORS:
        for el in soup.select(css):
            el.decompose()


def _collect_links(el, base: str) -> list[tuple[str, str]]:
    out = []
    for a in el.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(base, href)
        anchor = _text(a)
        out.append((absolute, anchor))
    return out


def _bold_cover(el, txt: str) -> float:
    if not txt:
        return 0.0
    bold = " ".join(_text(b) for b in el.find_all(["strong", "b"]))
    return len(bold) / len(txt)


def is_pseudo_heading(el, txt: str) -> bool:
    if el.name != "p":
        return False
    if len(txt.split()) > sel.PSEUDO_HEADING_MAX_WORDS:
        return False
    return _bold_cover(el, txt) >= sel.PSEUDO_HEADING_BOLD_COVER


def blocks_from_body(body, base: str) -> list[Block]:
    blocks, idx = [], 0
    for el in body.find_all(sel.BLOCK_TAGS):
        if el.find_parent(sel.BLOCK_TAGS) is not None:
            continue
        txt = _text(el)
        if not txt:
            continue
        kind = "h2" if is_pseudo_heading(el, txt) else el.name
        blocks.append(Block(index=idx, kind=kind, text=txt,
                            links=_collect_links(el, base)))
        idx += 1
    return blocks


def _meta(soup, name: str) -> str:
    tag = soup.find("meta", attrs={"name": name}) or \
        soup.find("meta", attrs={"property": name})
    return (tag.get("content") or "").strip() if tag else ""


def _find_abstract(soup) -> str:
    for css in sel.ABSTRACT_SELECTORS:
        el = soup.select_one(css)
        if el:
            t = _text(el)
            if len(t) > 80:
                return t
    for h in soup.find_all(["h1", "h2", "h3"]):
        if re.match(r"^\s*abstract\s*$", _text(h), re.I):
            parts, node = [], h.find_next_sibling()
            while node is not None and node.name not in ("h1", "h2", "h3"):
                if node.name in ("p", "div"):
                    parts.append(_text(node))
                node = node.find_next_sibling()
            joined = " ".join(p for p in parts if p)
            if len(joined) > 80:
                return joined
    return ""


def _event_dates(html: str) -> dict | None:
    try:
        soup = BeautifulSoup(html, "lxml")
        for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                payload = json.loads(tag.string or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            items = payload if isinstance(payload, list) else [payload]
            if isinstance(payload, dict) and "@graph" in payload:
                items = payload["@graph"]
            for item in items:
                if not isinstance(item, dict):
                    continue
                t = item.get("@type", "")
                types = t if isinstance(t, list) else [t]
                if any("event" in str(x).lower() for x in types):
                    if item.get("startDate") or item.get("endDate"):
                        return {
                            "startDate": item.get("startDate"),
                            "endDate": item.get("endDate") or item.get("startDate"),
                            "name": item.get("name"),
                        }
    except Exception:
        return None
    return None


BRAND_TOKENS = {"the cancer news", "binaytara", "ijccd", "binaytara foundation"}


def strip_brand(title: str) -> str:
    if not title:
        return ""
    parts = re.split(r"\s*[|\u2502]\s*", title)
    kept = [p.strip() for p in parts if p.strip().lower() not in BRAND_TOKENS]
    out = " | ".join(p for p in kept if p)
    return re.sub(r"\s+", " ", out).strip(" |\u2502-").strip() or title.strip()


def _sig_words(text: str) -> set[str]:
    """Significant words for consistency checking."""
    stop = {"the", "and", "for", "with", "from", "that", "this", "what", "how",
            "are", "was", "cancer", "new", "your", "you", "can", "все"}
    return {w for w in re.findall(r"[a-z]+", (text or "").lower())
            if len(w) > 3 and w not in stop}


def h1_is_consistent(h1: str, title: str, url: str) -> tuple[bool, str]:
    """Detect the CMS bug where a page serves an H1 belonging to a DIFFERENT
    article while its <title> and canonical are correct.

    Measured on binaytara.org in September 2026: roughly 10% of TCN articles
    (4 of a 40-article random sample) served a foreign H1. Example: the page at
    /cancernews/article/kidney-cancer-dual-io-hif2a-tki-free-intervals returned
    <title>Kidney Cancer in 2026...</title> with
    <h1>Reaching Out-of-School Maasai Girls With HPV Vaccination...</h1>.

    Storing that H1 is what produced the 'ghost data' and 'URL/title mismatch'
    findings in the writer review: the tool was faithfully reporting a value the
    CMS got wrong. Until the template is fixed, trust the title over the H1
    whenever the H1 shares no vocabulary with the slug but the title does.

    Returns (is_consistent, reason).
    """
    if not h1 or not title:
        return True, ""            # nothing to contradict
    slug = (url or "").rstrip("/").split("/")[-1]
    slug_w = _sig_words(slug.replace("-", " "))
    h1_w = _sig_words(h1)
    title_w = _sig_words(title)

    if not h1_w or not title_w:
        return True, ""

    # Signal A: the H1 and the <title> describe different articles. On a healthy
    # page these two always overlap heavily; they are generated from the same
    # field. Near-zero overlap is the clearest evidence of the CMS bug and does
    # not depend on how the slug happens to be worded.
    h1_title = len(h1_w & title_w) / min(len(h1_w), len(title_w))
    if len(h1_w) >= 3 and len(title_w) >= 3 and h1_title < 0.15:
        return False, (f"H1 and page title share no vocabulary "
                       f"({h1_title:.0%} overlap). The CMS is serving an H1 from a "
                       "different article.")

    # Signal B: relative to the URL slug, the title matches and the H1 does not.
    # Catches cases where H1 and title share an incidental common word.
    if slug_w:
        h1_slug = len(slug_w & h1_w) / len(slug_w)
        title_slug = len(slug_w & title_w) / len(slug_w)
        if h1_slug < 0.20 and title_slug >= 0.20 and title_slug > h1_slug:
            return False, (f"H1 does not match the URL slug but the title does "
                           f"(slug overlap: H1 {h1_slug:.0%}, title {title_slug:.0%}). "
                           "The CMS is serving an H1 from a different article.")
    return True, ""


def person_name_variants(full_name: str) -> list[str]:
    full_name = re.sub(r"\s+", " ", (full_name or "").strip())
    full_name = re.sub(r",?\s*(MD|PhD|DO|MPH|RN|DNB|MBBS)\b\.?", "", full_name, flags=re.I).strip()
    if not full_name:
        return []
    out = {full_name, f"Dr. {full_name}", f"Dr {full_name}"}
    parts = full_name.split()
    if len(parts) == 3 and re.fullmatch(r"[A-Z]\.?", parts[1]):
        out.add(f"{parts[0]} {parts[2]}")
    if len(parts) == 2:
        out.add(f"SURNAME_ONLY::{parts[1]}")
    return sorted(out)


def extract(html: str, final_url: str) -> dict | None:
    """Return a page record, or None when the page has no usable body.

    v3: includes _body_text (full body prose, not lowercased) so the indexer can
    store it for the keyword scanner.
    """
    soup = BeautifulSoup(html, "lxml")

    title = _text(soup.title) if soup.title else ""
    og_title = _meta(soup, "og:title")
    h1_el = soup.find("h1")
    h1 = _text(h1_el) if h1_el else ""
    meta_desc = _meta(soup, "description")
    placeholder = meta_desc.strip().lower() == sel.IJCCD_PLACEHOLDER_META
    canonical_el = soup.find("link", rel="canonical")
    canonical = (canonical_el.get("href") or "").strip() if canonical_el else ""

    all_links = _collect_links(soup, final_url)

    body = pick_body(soup)
    body_available = body is not None
    blocks = []
    if body_available:
        blocks = blocks_from_body(body, final_url)
        classify(blocks, meta_description="" if placeholder else meta_desc)

    body_links, seen = [], set()
    for b in blocks:
        for u, _anchor in b.links:
            n = ur.normalise(u)
            if n and ur.is_allowed(n) and n not in seen:
                seen.add(n)
                body_links.append(n)

    nav_links = []
    for u, _a in all_links:
        n = ur.normalise(u)
        if n and ur.is_allowed(n) and n not in seen:
            nav_links.append(n)

    section = ur.section_of(final_url)
    abstract = _find_abstract(soup) if (placeholder or not meta_desc) else ""
    event = _event_dates(html) if section == "Conference" else None

    body_text = " ".join(b.text for b in blocks)
    if not body_available:
        main = soup.find("main") or soup.body
        body_text = _text(main)[:2000] if main else ""
    digest = hashlib.sha256(
        "\n".join([h1, title, meta_desc, body_text]).encode("utf-8")
    ).hexdigest()

    title_clean = strip_brand(title or og_title)

    # v4: guard against the CMS serving a foreign H1 (see h1_is_consistent).
    h1_ok, h1_reason = h1_is_consistent(h1, title, final_url)
    h1_stored = h1 if h1_ok else ""
    if not h1_ok:
        # Keep the raw value for reporting, but do NOT let it into h1, where it
        # would become the displayed page title and an anchor-text source.
        h1_suspect = h1
    else:
        h1_suspect = ""

    names = person_name_variants(h1_stored or title_clean) if section == "Contributor" else []

    return {
        "url": final_url,
        "canonical_url": ur.normalise(canonical) if canonical else None,
        "section": section,
        "body_available": body_available,
        "title": title,
        "title_clean": title_clean,
        "h1": h1_stored,
        "h1_suspect": h1_suspect,
        "h1_mismatch_reason": h1_reason,
        "meta_description": "" if placeholder else meta_desc,
        "meta_description_is_placeholder": placeholder,
        "abstract": abstract,
        "abstract_missing": bool((placeholder or not meta_desc) and not abstract),
        "published_date": _meta(soup, "article:published_time"),
        "modified_date": _meta(soup, "article:modified_time"),
        "word_count": len(body_text.split()),
        "body_internal_links": body_links,
        "nav_internal_links": nav_links,
        "event": event,
        "person_names": names,
        "inbound_link_count": 0,
        "content_hash": digest,
        # v3: full body text for keyword scanner storage
        "_body_text": body_text,
        "_blocks": [
            {
                "index": b.index, "kind": b.kind, "text": b.text,
                "eligible": b.eligible, "skip_reason": b.skip_reason,
                "heading_context": b.heading_context,
            }
            for b in blocks
        ],
    }
