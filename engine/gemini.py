"""Gemini API client for page relevance judgment (v6).

Gemini's ONE job: answer 'Is this candidate page relevant to this article?
YES or NO.' Everything else (anchor selection, sentence finding, formatting)
is handled by code.

Uses Gemini 1.5 Flash for the highest free-tier limits (1,500 RPD, 15 RPM).
Falls back gracefully when no API key is configured or rate limits are hit.
"""
from __future__ import annotations

import json
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
    """Build the Give-side relevance judgment prompt."""
    h1 = article.get("h1") or article.get("title_clean") or ""
    diseases = ", ".join(article.get("_diseases", [])) or "unknown"
    existing = ", ".join(article.get("body_internal_links", [])[:10])

    cand_lines = []
    for i, c in enumerate(candidates, 1):
        line = (f"{i}. {c['url']} | H1: {c.get('h1') or c.get('title_clean') or ''} "
                f"| Section: {c.get('section', '')} "
                f"| Desc: {(c.get('meta_description') or '')[:100]}")
        cand_lines.append(line)

    return f"""You are reviewing internal link candidates for a cancer education website (binaytara.org).

ARTICLE: {h1}
URL: {article.get('url', '')}
DISEASES DISCUSSED: {diseases}
ALREADY LINKS TO: {existing}

ANSWER YES or NO for each candidate page below. Use these rules:

1. YES only if the page topic directly relates to a disease or subject this article discusses IN DEPTH (not just a passing mention in a list of 3+ cancers).
2. NO for a different cancer type unless the article explicitly discusses both (e.g., an alcohol article discusses both breast and esophageal cancer).
3. NO for event announcements, organizational pages, volunteer pages, donation pages.
4. NO for IJCCD journal papers UNLESS the study directly investigates a topic this article covers.
5. NO for treatment/clinical-trial pages when the article discusses risk/epidemiology of that cancer (topic mismatch). YES for overview/awareness/101 pages from risk sentences.
6. NO for pages the article already links to.
7. NO for pages whose content type does not match the article's discussion of that topic.

CANDIDATES:
{chr(10).join(cand_lines)}

RESPOND with ONLY numbered lines, one per candidate:
1. YES - [one line reason] or 1. NO - [one line reason]
Do not add any other text."""


def _build_receive_prompt(article: dict, candidates: list[dict]) -> str:
    """Build the Receive-side relevance judgment prompt."""
    h1 = article.get("h1") or article.get("title_clean") or ""
    diseases = ", ".join(article.get("_diseases", [])) or "unknown"

    cand_lines = []
    for i, c in enumerate(candidates, 1):
        line = (f"{i}. {c['url']} | H1: {c.get('h1') or c.get('title_clean') or ''} "
                f"| Section: {c.get('section', '')}")
        cand_lines.append(line)

    return f"""You are reviewing which pages should add an internal link TO the article below.

ARTICLE TO LINK TO: {h1}
URL: {article.get('url', '')}
TOPIC: {diseases}

Should each candidate page below add a link pointing to this article? Rules:

1. YES only if the candidate page discusses a topic where this article adds value (e.g., a breast cancer page that mentions alcohol should link to an alcohol-cancer-risk article).
2. NO for IJCCD journal papers (they cannot be edited to add links).
3. NO for pages that already link to this article.
4. NO if the connection is too thin (e.g., both mention 'cancer' but discuss unrelated topics).

CANDIDATES:
{chr(10).join(cand_lines)}

RESPOND with ONLY numbered lines:
1. YES - [reason] or 1. NO - [reason]"""


def _call_gemini(prompt: str) -> str | None:
    """Make one Gemini API call. Returns the text response or None on failure."""
    key = _get_api_key()
    if not key:
        return None

    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{settings.GEMINI_MODEL}:generateContent?key={key}")

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 2048,
        }
    }

    for attempt in range(3):
        try:
            r = httpx.post(url, json=payload, timeout=settings.GEMINI_TIMEOUT)
            if r.status_code == 200:
                data = r.json()
                return data["candidates"][0]["content"]["parts"][0]["text"]
            elif r.status_code == 429:
                # Rate limited. Wait and retry.
                wait = min(30, 5 * (attempt + 1))
                time.sleep(wait)
                continue
            else:
                return None
        except Exception:
            if attempt < 2:
                time.sleep(2)
            continue
    return None


def _parse_judgments(response: str, count: int) -> list[dict]:
    """Parse 'N. YES - reason' or 'N. NO - reason' lines."""
    results = []
    if not response:
        return [{"approved": False, "reason": "Gemini unavailable"} for _ in range(count)]

    lines = response.strip().split("\n")
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # Match patterns like "1. YES - reason" or "1. NO - reason"
        m = re.match(r"^\d+\.\s*(YES|NO)\s*[-–—:]\s*(.*)", line, re.I)
        if m:
            results.append({
                "approved": m.group(1).upper() == "YES",
                "reason": m.group(2).strip(),
            })

    # Pad if parsing missed some lines
    while len(results) < count:
        results.append({"approved": False, "reason": "Could not parse Gemini response"})

    return results[:count]


def judge_give_candidates(article: dict, candidates: list[dict]) -> list[dict]:
    """Ask Gemini which candidate pages are relevant for outbound links.

    Returns the candidates list with 'gemini_approved' and 'gemini_reason' added.
    """
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
    """Ask Gemini which candidate pages should link TO this article.

    Returns the candidates list with 'gemini_approved' and 'gemini_reason' added.
    """
    if not candidates:
        return candidates

    prompt = _build_receive_prompt(article, candidates)
    response = _call_gemini(prompt)
    judgments = _parse_judgments(response, len(candidates))

    for cand, j in zip(candidates, judgments):
        cand["gemini_approved"] = j["approved"]
        cand["gemini_reason"] = j["reason"]

    return candidates
