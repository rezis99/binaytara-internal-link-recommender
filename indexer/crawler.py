"""Async crawler.

Redirect handling is mandatory: httpx does NOT follow redirects by default, and
indexing a page under a stale URL would inject redirect hops into the very link
graph this tool exists to clean up.
"""
from __future__ import annotations

import asyncio

import httpx

from config import settings
from config import url_rules as ur

HEADERS = {"User-Agent": settings.USER_AGENT, "Accept": "text/html,*/*"}


class CrawlStats:
    def __init__(self) -> None:
        self.ok = 0
        self.failed = 0
        self.redirected = 0
        self.off_domain = 0
        self.rate_limited = 0


async def _fetch_one(client, url, sem, stats, state):
    async with sem:
        delay = 1.0
        for attempt in range(settings.MAX_RETRIES):
            try:
                r = await client.get(url)
            except httpx.HTTPError:
                await asyncio.sleep(delay)
                delay *= 2
                continue

            if r.status_code == 429:
                stats.rate_limited += 1
                state["n429"] += 1
                wait = float(r.headers.get("Retry-After", delay))
                await asyncio.sleep(min(wait, 30.0))
                delay *= 2
                continue

            if r.status_code == 403 and len(r.text) < 2000:
                state["waf"] += 1

            if r.status_code != 200:
                stats.failed += 1
                return None

            ctype = r.headers.get("content-type", "")
            if "text/html" not in ctype:
                stats.failed += 1
                return None

            if len(r.content) > settings.MAX_RESPONSE_BYTES:
                stats.failed += 1
                return None

            final = ur.normalise(str(r.url))
            if not final or not ur.is_allowed(final):
                stats.off_domain += 1
                return None

            chain = [str(h.url) for h in r.history]
            if chain:
                stats.redirected += 1

            stats.ok += 1
            return {
                "sitemap_url": url,
                "final_url": final,
                "redirect_chain": chain,
                "html": r.text,
            }
        stats.failed += 1
        return None


async def _run(urls: list[str], concurrency: int):
    stats = CrawlStats()
    state = {"n429": 0, "waf": 0}
    sem = asyncio.Semaphore(concurrency)
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=10)
    timeout = httpx.Timeout(settings.REQUEST_TIMEOUT)
    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, limits=limits, timeout=timeout,
        max_redirects=settings.MAX_REDIRECTS,
    ) as client:
        tasks = [_fetch_one(client, u, sem, stats, state) for u in urls]
        results = []
        for i, coro in enumerate(asyncio.as_completed(tasks), 1):
            results.append(await coro)
            if i % 100 == 0:
                print(f"  fetched {i}/{len(urls)}")
    return [r for r in results if r], stats, state


def crawl(urls: list[str], concurrency: int | None = None):
    """Returns (pages, stats). Raises on a probable WAF block rather than
    committing a half-built index."""
    concurrency = concurrency or settings.MAX_CONCURRENCY
    pages, stats, state = asyncio.run(_run(urls, concurrency))
    if state["waf"] > 5:
        raise RuntimeError(
            f"{state['waf']} probable WAF blocks (403 with empty body). "
            "Aborting rather than building a truncated index."
        )
    if state["n429"] > 3:
        print(f"  ! {state['n429']} rate-limit responses; consider lowering MAX_CONCURRENCY")
    print(f"  ok={stats.ok} failed={stats.failed} redirected={stats.redirected} "
          f"off_domain={stats.off_domain}")
    return pages, stats
