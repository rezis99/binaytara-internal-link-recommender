"""Sitemap index expansion to a filtered URL list."""
from __future__ import annotations

import httpx
from bs4 import BeautifulSoup

from config import settings
from config import url_rules as ur

HEADERS = {"User-Agent": settings.USER_AGENT}


def _fetch(client: httpx.Client, url: str) -> str | None:
    try:
        r = client.get(url, timeout=settings.REQUEST_TIMEOUT, follow_redirects=True)
        if r.status_code == 200:
            return r.text
    except httpx.HTTPError:
        return None
    return None


def _parse(xml: str) -> tuple[list[str], list[dict]]:
    soup = BeautifulSoup(xml, "xml")
    child = [loc.get_text(strip=True) for loc in soup.select("sitemap > loc")]
    urls = []
    for u in soup.select("url"):
        loc = u.find("loc")
        if not loc:
            continue
        lastmod = u.find("lastmod")
        urls.append({
            "sitemap_url": loc.get_text(strip=True),
            "lastmod": lastmod.get_text(strip=True) if lastmod else None,
        })
    return child, urls


def discover(entry: str | None = None) -> list[dict]:
    """Handles both a sitemap index and a flat sitemap at the entry URL."""
    entry = entry or settings.SITEMAP_URL
    seen_sitemaps, queue, records = set(), [entry], {}
    with httpx.Client(headers=HEADERS, follow_redirects=True) as client:
        while queue:
            sm = queue.pop(0)
            if sm in seen_sitemaps:
                continue
            seen_sitemaps.add(sm)
            xml = _fetch(client, sm)
            if not xml:
                print(f"  ! could not read sitemap {sm}")
                continue
            children, urls = _parse(xml)
            queue.extend(children)
            for rec in urls:
                n = ur.normalise(rec["sitemap_url"])
                if n and ur.is_allowed(n):
                    records.setdefault(n, rec)
    print(f"  sitemaps read: {len(seen_sitemaps)}; allowed URLs: {len(records)}")
    return list(records.values())
