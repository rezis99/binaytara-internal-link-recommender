"""Gemini API client (v6).

Three prompt types, each doing ONE job:
  1. Give judgment: Is this page a good link target for this article? YES/NO
  2. Receive judgment: Should this page link TO this article? YES/NO
  3. Sentence rewrite: Modify one sentence to include an anchor phrase naturally

Uses Gemini 1.5 Flash (1,500 RPD free, 15 RPM).
"""
from __future__ import annotations

import os
import re
import time
from functools import lru_cache

import httpx

from config import settings


@lru_cache(maxsize=1)
def _get_api_key() -> str | None:
    key = os.environ.get("GEMINI_API_KEY")
    if key:
        return key
    try:
        import streamlit as st
        return st.secrets.get("GEMINI_API_KEY")
    except Exception:
        return None


def is_available() -> tuple[bool, str]:
    key = _get_api_key()
    if key:
        return True, "Gemini 1.5 Flash"
    return False, "no GEMINI_API_KEY configured"


def _build_give_prompt(article: dict, candidates: list[dict]) -> str:
    h1 = article.get("h1") or article.get("title_clean") or ""
    diseases = ", ".join(article.get("_diseases", [])) or "not identified"
    existing = ", ".join(
        u.replace("https://binaytara.org", "")
        for u in (article.get("body_internal_links") or [])[:10]
    ) or "none"

    cand_lines = []
    for i, c in enumerate(candidates, 1):
        h = c.get("h1") or c.get("title_clean") or ""
        s = c.get("section", "")
        cand_lines.append(f"{i}. [{s}] {h}")

    return f"""TASK: Decide which candidate pages are good internal link targets for this article.

CONTEXT:
Site: binaytara.org (cancer education nonprofit)
Article: "{h1}"
Diseases this article covers: {diseases}
Pages already linked from this article: {existing}

DECISION RULES (apply strictly):

YES when:
- The candidate covers the SAME disease this article discusses individually (not in a list of 3+ cancers)
- The candidate is a 101/overview/awareness page for that disease
- The candidate is a relevant IJCCD study about that specific disease AND the risk factor or treatment this article covers
- The candidate is a contributor profile for a doctor quoted in this article

NO when:
- DIFFERENT DISEASE. "Breast cancer" is not "kidney cancer" even if both mention "immunotherapy"
- TOPIC MISMATCH. Do not suggest a treatment/clinical-trial page when the article discusses risk or epidemiology of that cancer. Risk sentences should link to overview/awareness/101 pages only
- ALREADY LINKED. The article already links to this page
- EVENT ANNOUNCEMENT. Blog posts about conferences, fundraisers, or organizational events
- ORGANIZATIONAL PAGE. Volunteer, donation, partnership, community health assessment pages
- IJCCD MISMATCH. Journal paper about a different topic than this article covers
- The candidate page has no topical connection to any paragraph in this article

CANDIDATES:
{chr(10).join(cand_lines)}

RESPOND exactly {len(candidates)} lines, one per candidate:
1. YES - [reason] or 1. NO - [reason]
Nothing else."""


def _build_receive_prompt(article: dict, candidates: list[dict]) -> str:
    h1 = article.get("h1") or article.get("title_clean") or ""
    diseases = ", ".join(article.get("_diseases", [])) or "not identified"

    cand_lines = []
    for i, c in enumerate(candidates, 1):
        h = c.get("h1") or c.get("title_clean") or ""
        s = c.get("section", "")
        cand_lines.append(f"{i}. [{s}] {h}")

    return f"""TASK: Decide which candidate pages should add an internal link pointing TO this article.

CONTEXT:
Article to link TO: "{h1}"
Article topic: {diseases}

The link on the candidate page should help THAT PAGE's readers discover this article. The anchor text will describe this article's topic, not just any keyword.

DECISION RULES:

YES when:
- The candidate page discusses a disease or risk factor that this article covers in depth
- The candidate page mentions a substance (alcohol, smoking, etc.) that this article analyzes
- Adding a link from that page to this article genuinely helps that page's readers understand the topic better

NO when:
- IJCCD journal paper (published research cannot be edited)
- ALREADY LINKS to this article
- CONNECTION TOO THIN. Sharing the word "cancer" alone is not enough. The candidate must discuss a topic this article covers specifically
- CONTRIBUTOR PROFILE (unless the contributor is quoted in this article)
- EVENT ANNOUNCEMENT or organizational page

CANDIDATES:
{chr(10).join(cand_lines)}

RESPOND exactly {len(candidates)} lines:
1. YES - [reason] or 1. NO - [reason]
Nothing else."""


def _build_rewrite_prompt(sentence: str, anchor: str, target_title: str,
                          target_url: str) -> str:
    return f"""TASK: Rewrite one sentence to include a hyperlinked phrase.

ORIGINAL: {sentence}

INSERT THIS PHRASE: {anchor}
LINK TO: {target_url} (page titled: {target_title})

RULES:
1. The phrase "{anchor}" must appear WORD FOR WORD in your output
2. Wrap it as [{anchor}]({target_url})
3. Keep the original meaning intact. Do not add facts, statistics, or medical claims
4. Minimal changes. If you can add just 2 to 3 words to fit the phrase, do that. Do not restructure the whole sentence
5. The result must read naturally as part of a cancer education article
6. Return ONLY the rewritten sentence. No quotes, no explanation, no preamble

REWRITE:"""


def _call_gemini(prompt: str, max_tokens: int = 2048) -> str | None:
    key = _get_api_key()
    if not key:
        return None

    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{settings.GEMINI_MODEL}:generateContent?key={key}")

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": max_tokens,
        }
    }

    for attempt in range(3):
        try:
            r = httpx.post(url, json=payload, timeout=settings.GEMINI_TIMEOUT)
            if r.status_code == 200:
                data = r.json()
                parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
                return parts[0].get("text", "") if parts else None
            elif r.status_code == 429:
                time.sleep(min(30, 5 * (attempt + 1)))
                continue
            else:
                return None
        except Exception:
            if attempt < 2:
                time.sleep(2)
            continue
    return None


def _parse_judgments(response: str, count: int) -> list[dict]:
    results = []
    if not response:
        return [{"approved": False, "reason": "Gemini unavailable"} for _ in range(count)]

    for line in response.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^\d+\.\s*(YES|NO)\s*[-\u2013\u2014:]\s*(.*)", line, re.I)
        if m:
            results.append({
                "approved": m.group(1).upper() == "YES",
                "reason": m.group(2).strip(),
            })

    while len(results) < count:
        results.append({"approved": False, "reason": "Parse error"})
    return results[:count]


def judge_give_candidates(article: dict, candidates: list[dict]) -> list[dict]:
    if not candidates:
        return candidates
    prompt = _build_give_prompt(article, candidates)
    response = _call_gemini(prompt)
    judgments = _parse_judgments(response, len(candidates))
    for cand, j in zip(candidates, judgments):
        cand["gemini_approved"] = j["approved"]
        cand["gemini_reason"] = j["reason"]
    return candidates


def judge_receive_candidates(article: dict, candidates: list[dict]) -> list[dict]:
    if not candidates:
        return candidates
    prompt = _build_receive_prompt(article, candidates)
    response = _call_gemini(prompt)
    judgments = _parse_judgments(response, len(candidates))
    for cand, j in zip(candidates, judgments):
        cand["gemini_approved"] = j["approved"]
        cand["gemini_reason"] = j["reason"]
    return candidates


def rewrite_sentence(sentence: str, anchor: str, target_title: str,
                     target_url: str) -> str | None:
    """Rewrite one sentence to include an anchor phrase naturally.

    Called ONLY when the anchor does not appear in the sentence.
    Code handles exact matches; Gemini handles creative rewriting.
    """
    key = _get_api_key()
    if not key:
        return None

    prompt = _build_rewrite_prompt(sentence, anchor, target_title, target_url)
    response = _call_gemini(prompt, max_tokens=512)
    if not response:
        return None

    rewrite = response.strip().strip('"').strip("'")

    # Strict validation
    if not re.search(r"\b" + re.escape(anchor) + r"\b", rewrite, re.I):
        return None
    if f"]({target_url})" not in rewrite:
        return None
    orig_words = len(sentence.split())
    new_words = len(rewrite.split())
    if orig_words > 0 and (new_words < orig_words * 0.6 or new_words > orig_words * 1.8):
        return None
    for bad in ["here is", "note:", "rewritten:", "i've", "i have"]:
        if bad in rewrite.lower():
            return None
    return rewrite
