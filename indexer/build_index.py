"""Full index build. Writes to a temp directory and only promotes artifacts into
data/ when every step succeeds, so a partial index is never committed.

v3 changes
----------
1. CANONICAL COLLISION FIX. Pages are always indexed under their actual (final)
   URL, never under a canonical that points elsewhere. A page whose canonical
   differs from its own URL is logged as a mismatch for SEO review, but its data
   is never overwritten by a different page. This eliminates the ghost-data bug
   that caused URL/title mismatches in 5 of 6 audit files.

2. BODY TEXT STORAGE. Each page's lowercased body text is written to
   body_texts.json so the keyword scanner can grep it at query time without
   re-crawling. Adds ~8 MB to the index for 1,680 pages.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import shutil
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import bm25s
import faiss
import httpx
import numpy as np

from config import settings
from config import url_rules as ur
from indexer import chunker, crawler, embedder, sitemap
from indexer.extractor import extract

ARTIFACTS = ["faiss.index", "pages.json", "paragraphs.json", "manifest.json",
             "anchor_guide.json", "body_texts.json", "bm25"]

CRAWL_FAILURE_LIMIT = 0.10
EXTRACTION_FAILURE_LIMIT = 0.25
MIN_RETAINED_RATIO = 0.80


def fetch_anchor_guide() -> dict:
    """Supplementary data source. A failure here must never fail the build."""
    guide: dict[str, dict] = {}
    try:
        r = httpx.get(settings.ANCHOR_GUIDE_CSV, timeout=30, follow_redirects=True)
        r.raise_for_status()
        reader = csv.reader(io.StringIO(r.text))
        rows = list(reader)
        if not rows:
            return guide
        header = [h.strip().lower() for h in rows[0]]
        url_i = next((i for i, h in enumerate(header) if "url" in h or "target" in h), None)
        anc_i = next((i for i, h in enumerate(header) if "anchor" in h), None)
        if url_i is None or anc_i is None:
            print("  ! anchor guide columns not recognised; skipping")
            return guide
        for row in rows[1:]:
            if len(row) <= max(url_i, anc_i):
                continue
            u = ur.normalise(row[url_i].strip())
            a = row[anc_i].strip()
            if u and a:
                guide.setdefault(u, {"approved_anchors": [], "source": "anchor_guide"})
                if a not in guide[u]["approved_anchors"]:
                    guide[u]["approved_anchors"].append(a)
        print(f"  anchor guide: {len(guide)} target URLs")
    except Exception as exc:                                  # noqa: BLE001
        print(f"  ! anchor guide unavailable ({exc}); continuing without it")
    return guide


def build(limit: int | None = None, out_dir: Path | None = None,
          per_section: int | None = None) -> dict:
    started = datetime.now(timezone.utc)
    tmp = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="bil-index-"))
    tmp.mkdir(parents=True, exist_ok=True)

    print("1/7 discovering URLs from sitemap")
    records = sitemap.discover()
    if per_section:
        buckets: dict[str, list] = {}
        for r in records:
            buckets.setdefault(ur.section_of(r["sitemap_url"]), []).append(r)
        records = [r for rows in buckets.values() for r in rows[:per_section]]
        print(f"  stratified sample: {len(records)} URLs "
              f"({ {k: min(len(v), per_section) for k, v in buckets.items()} })")
    elif limit:
        records = records[:limit]
    lastmod = {ur.normalise(r["sitemap_url"]): r.get("lastmod") for r in records}
    urls = [ur.normalise(r["sitemap_url"]) for r in records]

    print(f"2/7 crawling {len(urls)} pages")
    fetched, stats = crawler.crawl(urls)
    if not fetched:
        raise RuntimeError("no pages fetched; aborting")
    if stats.failed > CRAWL_FAILURE_LIMIT * len(urls):
        raise RuntimeError(
            f"{stats.failed}/{len(urls)} pages failed (>10%); aborting without committing"
        )

    print("3/7 extracting content")
    pages, all_chunks, redirects = {}, [], {}
    body_texts: dict[str, str] = {}
    extraction_failed = []
    canonical_mismatches = []
    canonical_collisions = []
    h1_mismatches = []           # v4: CMS serving a foreign H1

    for item in fetched:
        rec = extract(item["html"], item["final_url"])
        if rec is None:
            extraction_failed.append(item["final_url"])
            continue

        actual_url = rec["url"]       # final URL after redirect following
        canonical = rec.get("canonical_url")

        # --- v3 CANONICAL COLLISION FIX ---
        # Always index under the actual URL. A canonical that points elsewhere
        # is logged but never followed, because following it caused Page A's
        # data to overwrite Page B's data when both had broken canonicals
        # pointing at B. This was the root cause of the URL/title mismatches
        # found in 5 of 6 audit files.
        target = actual_url

        if canonical and canonical != actual_url:
            if ur.is_allowed(canonical):
                canonical_mismatches.append({
                    "page": actual_url,
                    "canonical_points_to": canonical,
                    "action": "indexed under actual URL; canonical ignored"
                })
            # If the canonical points to a URL already in the index, that is
            # a collision we previously would have caused. Log it explicitly.
            if canonical in pages:
                canonical_collisions.append({
                    "page": actual_url,
                    "would_have_overwritten": canonical,
                })

        rec["url"] = target
        rec["sitemap_url"] = item["sitemap_url"]
        rec["redirect_chain"] = item["redirect_chain"]
        rec["lastmod"] = lastmod.get(item["sitemap_url"])

        for hop in item["redirect_chain"]:
            n = ur.normalise(hop)
            if n and n != target:
                redirects[n] = target

        # v4: record pages where the CMS served an H1 from another article.
        if rec.get("h1_suspect"):
            h1_mismatches.append({
                "url": target,
                "title": rec.get("title_clean", ""),
                "wrong_h1": rec.get("h1_suspect", ""),
                "reason": rec.get("h1_mismatch_reason", ""),
            })

        # Store body text for the keyword scanner (v3).
        raw_body = rec.pop("_body_text", "")
        if raw_body:
            body_texts[target] = raw_body.lower()

        blocks = rec.pop("_blocks")
        page_chunks = chunker.chunk_page(target, blocks)
        if not page_chunks:
            syn = chunker.synthetic_chunk(rec)
            if syn:
                page_chunks = [syn]
        all_chunks.extend(page_chunks)

        # True collision: two different actual URLs after redirect resolution.
        if target in pages:
            canonical_collisions.append({
                "page": item["final_url"],
                "collides_with": target,
                "action": "later fetch overwrites"
            })
        pages[target] = rec

    target_only = sum(1 for r in pages.values() if not r.get("body_available"))
    print(f"  pages={len(pages)} chunks={len(all_chunks)} redirects={len(redirects)} "
          f"target_only={target_only} extraction_failed={len(extraction_failed)} "
          f"canonical_mismatches={len(canonical_mismatches)} "
          f"canonical_collisions={len(canonical_collisions)} "
          f"h1_mismatches={len(h1_mismatches)}")
    if not all_chunks:
        raise RuntimeError("no eligible chunks produced; check the selectors")

    if len(extraction_failed) > EXTRACTION_FAILURE_LIMIT * len(fetched):
        raise RuntimeError(
            f"{len(extraction_failed)}/{len(fetched)} pages fetched but produced no "
            f"usable record (>{EXTRACTION_FAILURE_LIMIT:.0%}). The site template has "
            "probably changed; check config/selectors.py. Aborting without committing.\n"
            f"  examples: {extraction_failed[:5]}"
        )
    if canonical_mismatches:
        print(f"  ! {len(canonical_mismatches)} pages have a canonical pointing elsewhere "
              "(indexed under their own URL)")
        for m in canonical_mismatches[:5]:
            print(f"      {m['page']} -> canonical says {m['canonical_points_to']}")
    if canonical_collisions:
        print(f"  ! {len(canonical_collisions)} canonical collisions detected and prevented")

    # Guard against a silent shrink.
    prev_path = settings.DATA / "manifest.json"
    if out_dir is None and prev_path.exists():
        try:
            prev = json.loads(prev_path.read_text("utf-8"))
            prev_pages = int(prev.get("pages", 0))
            if prev_pages and len(pages) < MIN_RETAINED_RATIO * prev_pages and not limit \
                    and not per_section:
                raise RuntimeError(
                    f"Page count dropped from {prev_pages} to {len(pages)} "
                    f"(<{MIN_RETAINED_RATIO:.0%} retained). Aborting without committing; "
                    "run with --per-section to investigate."
                )
        except json.JSONDecodeError:
            pass

    print("4/7 counting inbound links")
    inbound = defaultdict(int)
    for rec in pages.values():
        for tgt in set(rec["body_internal_links"]):
            tgt = redirects.get(tgt, tgt)
            if tgt in pages and tgt != rec["url"]:
                inbound[tgt] += 1
    for u, rec in pages.items():
        rec["inbound_link_count"] = inbound.get(u, 0)
    orphans = sum(1 for r in pages.values() if r["inbound_link_count"] == 0)
    print(f"  pages with zero inbound body links: {orphans}/{len(pages)}")

    print("5/7 embedding")
    texts = [chunker.embed_text(c) for c in all_chunks]
    mat = embedder.embed_passages(texts)
    embedder.assert_normalised(mat)
    index = faiss.IndexFlatIP(settings.EMBED_DIM)
    index.add(mat)
    faiss.write_index(index, str(tmp / "faiss.index"))

    print("6/7 building lexical index and writing artifacts")
    corpus_tokens = bm25s.tokenize(texts, stopwords="en", show_progress=False)
    retriever = bm25s.BM25()
    retriever.index(corpus_tokens, show_progress=False)
    retriever.save(str(tmp / "bm25"))

    guide = fetch_anchor_guide()

    print("7/7 writing body texts and manifest")
    manifest = {
        "version": 3,
        "embedding_runtime": "fastembed",
        "model_name": settings.MODEL_NAME,
        "embedding_dimension": settings.EMBED_DIM,
        "normalization": True,
        "query_prefix_applied_by": settings.QUERY_PREFIX_APPLIED_BY,
        "faiss_version": faiss.__version__,
        "built_at": started.isoformat(),
        "pages": len(pages),
        "chunks": len(all_chunks),
        "body_texts_stored": len(body_texts),
        "orphan_pages": orphans,
        "target_only_pages": target_only,
        "extraction_failed": len(extraction_failed),
        "canonical_mismatches": canonical_mismatches[:20],
        "canonical_collisions": [
            {"from": c.get("page", ""), "detail": c}
            for c in canonical_collisions[:20]
        ],
        # v4: full list, not truncated. This is a CMS defect report that goes to
        # the dev team, so every affected URL has to be in it.
        "h1_mismatches": h1_mismatches,
        "h1_mismatch_count": len(h1_mismatches),
        "build_seconds": None,
        "redirects": redirects,
        "lastmod": {u: lastmod.get(pages[u].get("sitemap_url")) for u in pages},
        "content_hash": {u: r["content_hash"] for u, r in pages.items()},
    }

    (tmp / "pages.json").write_text(json.dumps(pages, ensure_ascii=False), "utf-8")
    (tmp / "paragraphs.json").write_text(json.dumps(all_chunks, ensure_ascii=False), "utf-8")
    (tmp / "body_texts.json").write_text(json.dumps(body_texts, ensure_ascii=False), "utf-8")
    (tmp / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), "utf-8")
    (tmp / "anchor_guide.json").write_text(json.dumps(guide, ensure_ascii=False), "utf-8")

    if out_dir is None:
        settings.DATA.mkdir(parents=True, exist_ok=True)
        for name in ARTIFACTS:
            src, dst = tmp / name, settings.DATA / name
            if not src.exists():
                continue
            if dst.exists():
                shutil.rmtree(dst) if dst.is_dir() else dst.unlink()
            shutil.move(str(src), str(dst))
        print(f"promoted artifacts into {settings.DATA}")
    took = (datetime.now(timezone.utc) - started).total_seconds()
    manifest["build_seconds"] = round(took)
    (settings.DATA if out_dir is None else tmp).joinpath("manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), "utf-8")
    print(f"done in {took:.0f}s")
    return manifest


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None,
                    help="index only the first N sitemap URLs (for smoke tests)")
    ap.add_argument("--per-section", type=int, default=None,
                    help="stratified smoke test: N URLs from each section")
    args = ap.parse_args()
    try:
        build(limit=args.limit, per_section=args.per_section)
    except Exception as exc:                                   # noqa: BLE001
        print(f"BUILD FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
