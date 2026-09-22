"""Conference expiry check (SOP rule R11).

Conference pages are only suggested when the tool is very confident AND the
event has not already ended.
"""
from __future__ import annotations

import re
from datetime import date, datetime


def _parse(value: str | None) -> date | None:
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(text[:len(fmt) + 2].rstrip("Z"), fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def slug_year(page: dict) -> int | None:
    """Fallback when Event schema is absent: the latest four-digit year found in
    the URL, H1 or title."""
    blob = " ".join([page.get("url", ""), page.get("h1", ""), page.get("title", "")])
    years = [int(y) for y in re.findall(r"\b(20\d{2})\b", blob)]
    return max(years) if years else None


def is_active(page: dict, today: date | None = None) -> tuple[bool, str]:
    """Return (usable, note). Conservative: drops on ambiguity."""
    today = today or date.today()
    event = page.get("event") or {}
    end = _parse(event.get("endDate")) or _parse(event.get("startDate"))
    if end:
        if end < today:
            return False, f"Event ended {end.isoformat()}"
        return True, f"Event runs to {end.isoformat()}"

    year = slug_year(page)
    if year is None:
        return False, "No Event schema and no year found; not suggested"
    if year < today.year:
        return False, f"No Event schema; year {year} in the page is in the past"
    return True, f"No Event schema; year {year} inferred from the page. Verify before linking"
