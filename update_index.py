"""Change detection plus full rebuild.

Named honestly: this module DETECTS what changed and then rebuilds the whole
corpus. It is not an incremental artifact update. At the current corpus size a
full rebuild takes minutes and removes a whole class of merge bugs, which is the
right trade. Revisit if build_seconds in the manifest starts approaching the
workflow timeout; the workflow already emits a warning above 30 minutes.

At roughly 10 new articles a week a scheduled run touches a handful of pages.
The FAISS index is rebuilt from the full chunk set each time: a flat index of
this size rebuilds in seconds, and incremental FAISS deletion is not worth the
complexity or the risk.
"""
from __future__ import annotations

import json
import sys

from config import settings
from config import url_rules as ur
from indexer import build_index, sitemap


def changed_urls() -> tuple[list[str], list[str]]:
    manifest_path = settings.DATA / "manifest.json"
    if not manifest_path.exists():
        return [], []
    manifest = json.loads(manifest_path.read_text("utf-8"))
    known_lastmod = manifest.get("lastmod", {})
    records = sitemap.discover()
    current = {ur.normalise(r["sitemap_url"]): r.get("lastmod") for r in records}
    new = [u for u in current if u not in known_lastmod]
    stale = [u for u, lm in current.items()
             if u in known_lastmod and (known_lastmod[u] is None or
                                        (lm or "") > (known_lastmod[u] or ""))]
    removed = [u for u in known_lastmod if u not in current]
    print(f"new={len(new)} changed={len(stale)} removed={len(removed)}")
    return new + stale, removed


def main() -> int:
    touched, removed = changed_urls()
    if not touched and not removed and (settings.DATA / "faiss.index").exists():
        print("Nothing changed; index left untouched.")
        return 0
    # A full rebuild at this corpus size takes a few minutes and removes a whole
    # class of merge bugs. Revisit only if the site grows by an order of magnitude.
    print("Rebuilding the full index.")
    build_index.build()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:                                   # noqa: BLE001
        print(f"UPDATE FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
