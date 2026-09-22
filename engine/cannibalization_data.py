"""Real keyword cannibalization from Semrush position data (v4).

v3 and earlier used an H1/title n-gram proxy and honestly labelled it a
heuristic, because comparing titles detects TOPICAL similarity, not query-level
competition. This module replaces the proxy with the real thing: two pages
cannibalize when they both rank for the same search query.

Input: a Semrush "Organic Positions" export (xlsx or csv) with at minimum the
columns Keyword, Position, URL, Search Volume, Position Type.

Output: data/cannibalization.json, a map of
    {url_a: {url_b: {"shared": n, "volume": v, "top_terms": [...]}}}

Regenerate whenever you pull a fresh Semrush export:

    python -m engine.cannibalization_data path/to/semrush-export.xlsx

The map is optional. When absent, the tool falls back to the v3 title heuristic
and says so in the output, rather than silently pretending it has query data.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from itertools import combinations
from pathlib import Path

from config import settings
from config import url_rules as ur

OUTPUT_NAME = "cannibalization.json"

# A pair sharing fewer than this many queries is noise, not competition.
MIN_SHARED_QUERIES = 2
# Cap the stored term list so the artifact stays small.
MAX_TOP_TERMS = 6


def build_from_export(path: str | Path, out_dir: Path | None = None) -> dict:
    """Parse a Semrush organic positions export into a cannibalization map."""
    import pandas as pd

    path = Path(path)
    if path.suffix.lower() in (".xlsx", ".xls"):
        df = pd.read_excel(path)
    else:
        df = pd.read_csv(path)

    required = {"Keyword", "Position", "URL"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            f"Export is missing required columns: {sorted(missing)}. "
            f"Found: {sorted(df.columns)}"
        )

    # Organic rows only. AI overview / People also ask / Image pack rows describe
    # the same underlying ranking and would double-count the pair.
    if "Position Type" in df.columns:
        df = df[df["Position Type"] == "Organic"]

    df = df.copy()
    df["Keyword"] = df["Keyword"].astype(str).str.strip().str.lower()
    df["URL"] = df["URL"].astype(str).str.strip().str.rstrip("/")
    vol_col = "Search Volume" if "Search Volume" in df.columns else None

    # Normalise URLs through the same gate the index uses, so the map keys match
    # what the tool will look up. Off-domain rows (education.binaytara.org and
    # other subdomains) are kept OUT: the tool never suggests them, so flagging
    # competition against them would be noise the writer cannot act on.
    def norm(u: str) -> str | None:
        n = ur.normalise(u)
        return n if (n and ur.is_allowed(n)) else None

    df["norm_url"] = df["URL"].map(norm)
    df = df[df["norm_url"].notna()]

    agg = {"pos": ("Position", "min")}
    if vol_col:
        agg["vol"] = (vol_col, "max")
    best = df.groupby(["Keyword", "norm_url"]).agg(**agg).reset_index()

    multi = best.groupby("Keyword")["norm_url"].nunique()
    multi = set(multi[multi > 1].index)

    pair_terms: dict[tuple[str, str], list] = defaultdict(list)
    for kw in multi:
        rows = best[best["Keyword"] == kw]
        urls = sorted(set(rows["norm_url"]))
        volume = int(rows["vol"].max()) if vol_col and rows["vol"].notna().any() else 0
        for a, b in combinations(urls, 2):
            pair_terms[(a, b)].append((kw, volume))

    out: dict[str, dict] = {}
    pairs_kept = 0
    for (a, b), terms in pair_terms.items():
        if len(terms) < MIN_SHARED_QUERIES:
            continue
        pairs_kept += 1
        terms.sort(key=lambda t: -t[1])
        payload = {
            "shared": len(terms),
            "volume": sum(v for _k, v in terms),
            "top_terms": [k for k, _v in terms[:MAX_TOP_TERMS]],
        }
        out.setdefault(a, {})[b] = payload
        out.setdefault(b, {})[a] = payload

    target = (out_dir or settings.DATA)
    target.mkdir(parents=True, exist_ok=True)
    (target / OUTPUT_NAME).write_text(json.dumps(out, ensure_ascii=False), "utf-8")

    print(f"cannibalization map written to {target / OUTPUT_NAME}")
    print(f"  competing pairs kept: {pairs_kept} (min {MIN_SHARED_QUERIES} shared queries)")
    print(f"  URLs involved: {len(out)}")
    return out


def load(data_dir: Path | None = None) -> dict:
    """Load the map. Returns {} when it has not been generated."""
    d = data_dir or settings.DATA
    try:
        return json.loads((d / OUTPUT_NAME).read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def assess(source_url: str, target_url: str, cmap: dict) -> tuple[str, str]:
    """Query-level competition between two pages. Returns (level, explanation).

    Unlike the title heuristic this measures what the name claims: whether the
    two pages actually rank for the same searches.
    """
    if not cmap:
        return "", ""
    entry = (cmap.get(source_url) or {}).get(target_url)
    if not entry:
        return "None", ""
    shared = entry["shared"]
    terms = ", ".join(entry.get("top_terms", [])[:3])
    if shared >= settings.CANNIBAL_HIGH_QUERIES:
        return "High", (f"These pages compete for {shared} of the same search queries "
                        f"({terms}). Linking between them concentrates the signal on one; "
                        "decide which should rank")
    if shared >= settings.CANNIBAL_MEDIUM_QUERIES:
        return "Medium", (f"These pages share {shared} ranking queries ({terms}). "
                          "Check which one you want to rank before linking")
    return "None", ""


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python -m engine.cannibalization_data <semrush-export.xlsx>",
              file=sys.stderr)
        sys.exit(1)
    try:
        build_from_export(sys.argv[1])
    except Exception as exc:                                   # noqa: BLE001
        print(f"FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
