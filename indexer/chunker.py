"""Eligible blocks to embeddable chunks, in strict document order."""
from __future__ import annotations

import pysbd

from config import settings

_seg = pysbd.Segmenter(language="en", clean=False)


def sentences(text: str) -> list[str]:
    try:
        out = [s.strip() for s in _seg.segment(text) if s and s.strip()]
    except Exception:
        out = [text.strip()]
    return out or [text.strip()]


def chunk_page(url: str, blocks: list[dict]) -> list[dict]:
    """One chunk per eligible block; long blocks split on sentence boundaries
    with a one-sentence overlap. chunk_id encodes document order so the
    retrieval collapse step has a deterministic tie-breaker."""
    chunks = []
    for b in blocks:
        if not b.get("eligible"):
            continue
        sents = sentences(b["text"])
        groups, cur, cur_words = [], [], 0
        for s in sents:
            w = len(s.split())
            if cur and cur_words + w > settings.MAX_CHUNK_WORDS:
                groups.append(cur)
                cur = [cur[-1]] if len(cur) > 1 else []      # one-sentence overlap
                cur_words = sum(len(x.split()) for x in cur)
            cur.append(s)
            cur_words += w
        if cur:
            groups.append(cur)

        for ordinal, g in enumerate(groups):
            text = " ".join(g).strip()
            if len(text.split()) < settings.MIN_BLOCK_WORDS and len(groups) > 1:
                continue
            chunks.append({
                "chunk_id": f"{url}#b{b['index']:04d}c{ordinal:02d}",
                "url": url,
                "block_index": b["index"],
                "chunk_ordinal": ordinal,
                "heading_context": b.get("heading_context", ""),
                "text": text,
                "sentences": g,
                "word_count": len(text.split()),
                "placement_ok": True,
                # v5b: carry the block's existing links so downstream code can
                # detect sentences that already contain a hyperlink.
                "existing_links": b.get("links", []),
            })
    return chunks


def embed_text(chunk: dict) -> str:
    """Heading context is prepended for embedding only; the displayed sentence
    stays clean."""
    h = (chunk.get("heading_context") or "").strip()
    return f"{h}. {chunk['text']}" if h else chunk["text"]


def synthetic_chunk(page: dict) -> dict:
    """A single retrievable chunk for a page that has no placeable prose (a hub
    or listing page). It lets the page be FOUND as a link target, while
    placement_ok=False keeps it out of the Links to Receive table, where a
    paragraph to edit is required."""
    parts = [page.get("h1") or "", page.get("title_clean") or "",
             page.get("meta_description") or "", page.get("abstract") or ""]
    text = ". ".join(p.strip() for p in parts if p and p.strip())
    if not text:
        return {}
    return {
        "chunk_id": f"{page['url']}#synthetic",
        "url": page["url"],
        "block_index": -1,
        "chunk_ordinal": 0,
        "heading_context": "",
        "text": text[:1200],
        "sentences": [text[:1200]],
        "word_count": len(text.split()),
        "placement_ok": False,
    }
