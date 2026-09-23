"""Gemini API client (v7).

Three structural fixes over v6, each driven by a specific failure:

1. MODEL AUTO-DISCOVERY. v6 hardcoded gemini-1.5-flash, which had been
   retired. Every call failed, and the fail-open fallback silently shipped
   an unfiltered file. v7 queries the real ListModels endpoint at runtime,
   prefers Flash-Lite variants (500 requests/day free, vs ~20/day for full
   Flash as of September 2026), and if a listed model still 404s on an
   actual generateContent call, tries the next candidate rather than
   failing silently.

2. MULTI-KEY ROTATION. Any number of GEMINI_API_KEY / GEMINI_API_KEY_1 /
   GEMINI_API_KEY_2... secrets are pooled. A 429 or 403 rotates to the next
   key instead of stalling the batch.

3. THE LLM OWNS ANCHOR SELECTION AND SENTENCE REWRITING, NOT CODE. v6's
   code sliced anchors out of the destination page's H1 with n-grams,
   which produced fragments like "Dr Gentry King on Advances" or "First
   Positive" -- grammatical outside their original title, meaningless
   outside it. v7 shows Gemini the real candidate sentences from the
   SOURCE article and asks it to pick the one place a link genuinely
   fits, choose a natural topic phrase, and write the complete modified
   sentence. Every response is validated (engine.link_validation) and
   retried up to 3 times with the specific failure fed back as feedback;
   if it still fails, the row is dropped rather than shown to a writer as
   a manual instruction.
"""
from __future__ import annotations

import json
import os
import re
import time
from functools import lru_cache

import httpx

from config import settings
from engine import link_validation as lv

_MODEL_PREFERENCE = [
    re.compile(r"^models/gemini-3\.\d+-flash-lite$"),
    re.compile(r"^models/gemini-3-flash-lite$"),
    re.compile(r"^models/gemini-\d+\.\d+-flash-lite$"),
    re.compile(r"^models/gemini-3\.\d+-flash$"),
    re.compile(r"^models/gemini-3-flash$"),
    re.compile(r"^models/gemini-\d+\.\d+-flash$"),
    re.compile(r"^models/gemini-\d+\.\d+-flash-lite$"),
]
_EXCLUDE = re.compile(r"(image|audio|native|embedding|vision|tts|robotics|"
                      r"live|thinking|preview-\d{2}-\d{4})", re.I)


# ---------------------------------------------------------------- keys

def _get_api_keys() -> list[str]:
    """GEMINI_API_KEY plus any GEMINI_API_KEY_1, _2, ... for rotation
    across separate Google accounts, each with its own free daily quota."""
    keys: list[str] = []
    seen: set[str] = set()

    def _add(v):
        if v and v not in seen:
            seen.add(v)
            keys.append(v)

    _add(os.environ.get("GEMINI_API_KEY"))
    i = 1
    while True:
        v = os.environ.get(f"GEMINI_API_KEY_{i}")
        if not v:
            break
        _add(v)
        i += 1

    try:
        import streamlit as st
        _add(st.secrets.get("GEMINI_API_KEY"))
        i = 1
        while True:
            v = st.secrets.get(f"GEMINI_API_KEY_{i}")
            if not v:
                break
            _add(v)
            i += 1
    except Exception:
        pass

    return keys


class _KeyPool:
    def __init__(self, keys):
        self.keys = keys
        self.cooldowns: dict[str, float] = {}
        self._idx = 0

    def available(self):
        now = time.time()
        return [k for k in self.keys if self.cooldowns.get(k, 0) <= now]

    def next_key(self):
        avail = self.available()
        if not avail:
            return None
        k = avail[self._idx % len(avail)]
        self._idx += 1
        return k

    def cool(self, key, seconds=60.0):
        self.cooldowns[key] = time.time() + seconds


@lru_cache(maxsize=1)
def _pool() -> _KeyPool:
    return _KeyPool(_get_api_keys())


def is_available() -> tuple[bool, str]:
    keys = _get_api_keys()
    if not keys:
        return False, "no GEMINI_API_KEY configured"
    model = _cached_model_for(keys[0])
    if model:
        label = model.replace("models/", "")
        suffix = f" ({len(keys)} keys)" if len(keys) > 1 else ""
        return True, f"{label}{suffix}"
    return False, f"{len(keys)} key(s) configured, but no usable model found"


# ---------------------------------------------------------------- model discovery

@lru_cache(maxsize=8)
def _list_models(api_key: str) -> tuple:
    try:
        r = httpx.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            params={"key": api_key, "pageSize": 200}, timeout=20)
        if r.status_code != 200:
            return ()
        data = r.json()
        out = []
        for m in data.get("models", []):
            name = m.get("name", "")
            methods = m.get("supportedGenerationMethods", [])
            if "generateContent" in methods and not _EXCLUDE.search(name):
                out.append(name)
        return tuple(out)
    except Exception:
        return ()


def _candidate_models(api_key: str) -> list:
    available = _list_models(api_key)
    ranked = []
    for pattern in _MODEL_PREFERENCE:
        for name in available:
            if pattern.match(name) and name not in ranked:
                ranked.append(name)
    for name in available:
        if name not in ranked:
            ranked.append(name)
    return ranked


_dead_models: set = set()


@lru_cache(maxsize=8)
def _cached_model_for(api_key: str):
    for name in _candidate_models(api_key):
        if name not in _dead_models:
            return name
    return None


# ---------------------------------------------------------------- calling

def _call_gemini(prompt: str, max_tokens: int = 2048,
                 temperature: float = 0.1):
    pool = _pool()
    tried = set()

    for _ in range(len(pool.keys) * 3 + 3):
        key = pool.next_key()
        if key is None:
            break
        model = _cached_model_for(key)
        if model is None:
            continue
        if (key, model) in tried:
            continue
        tried.add((key, model))

        url = f"https://generativelanguage.googleapis.com/v1beta/{model}:generateContent"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
        }
        try:
            r = httpx.post(url, params={"key": key}, json=payload,
                           timeout=settings.GEMINI_TIMEOUT)
        except Exception:
            continue

        if r.status_code == 200:
            data = r.json()
            parts = (data.get("candidates", [{}])[0]
                    .get("content", {}).get("parts", []))
            return parts[0].get("text", "") if parts else None
        if r.status_code == 404:
            _dead_models.add(model)
            _cached_model_for.cache_clear()
            continue
        if r.status_code in (429, 403):
            pool.cool(key, seconds=60.0)
            continue
        continue

    return None


# ---------------------------------------------------------------- relevance prompts

def _build_give_prompt(article, candidates):
    h1 = article.get("h1") or article.get("title_clean") or ""
    diseases = ", ".join(article.get("_diseases", [])) or "not identified"
    existing = ", ".join(
        u.replace("https://binaytara.org", "")
        for u in (article.get("body_internal_links") or [])[:10]
    ) or "none"
    cand_lines = [
        f"{i}. [{c.get('section', '')}] {c.get('h1') or c.get('title_clean') or ''}"
        for i, c in enumerate(candidates, 1)
    ]
    return f"""TASK: Decide which candidate pages are good internal link targets for this article.

CONTEXT:
Site: binaytara.org (cancer education nonprofit)
Article: "{h1}"
Diseases this article covers: {diseases}
Pages already linked from this article: {existing}

DECISION RULES:

YES when:
- Same disease this article discusses individually (not inside a list of 3+ cancers)
- 101/overview/awareness page for that disease
- IJCCD study directly about that disease AND the risk factor or treatment this article covers
- Contributor profile for a doctor quoted in this article

NO when:
- DIFFERENT DISEASE, even if both mention "immunotherapy" or another shared term
- TOPIC MISMATCH: risk/epidemiology sentence should not link to a treatment/clinical-trial page
- ALREADY LINKED from this article
- EVENT ANNOUNCEMENT, fundraiser, or organizational blog post
- IJCCD paper about an unrelated topic
- No real topical connection to this article

CANDIDATES:
{chr(10).join(cand_lines)}

RESPOND exactly {len(candidates)} lines:
1. YES - [reason] or 1. NO - [reason]
Nothing else."""


def _build_receive_prompt(article, candidates):
    h1 = article.get("h1") or article.get("title_clean") or ""
    diseases = ", ".join(article.get("_diseases", [])) or "not identified"
    cand_lines = [
        f"{i}. [{c.get('section', '')}] {c.get('h1') or c.get('title_clean') or ''}"
        for i, c in enumerate(candidates, 1)
    ]
    return f"""TASK: Decide which candidate pages should add a link pointing TO this article.

Article to link TO: "{h1}"
Topic: {diseases}

YES when the candidate page discusses a disease or risk factor this article covers in depth, and a link would genuinely help that page's readers.
NO for: IJCCD papers (cannot be edited), pages that already link here, thin connections (sharing only the word "cancer"), contributor profiles, event or organizational pages.

CANDIDATES:
{chr(10).join(cand_lines)}

RESPOND exactly {len(candidates)} lines:
1. YES - [reason] or 1. NO - [reason]
Nothing else."""


def _parse_judgments(response, count):
    if not response or not response.strip():
        return [{"approved": True, "reason": "Gemini unavailable; unfiltered"}
                for _ in range(count)]

    cleaned = response.replace("**", "").replace("__", "").replace("`", "")
    by_index = {}
    pattern = re.compile(
        r"^\s*(\d+)\s*[\.\)\-:]?\s*(YES|NO)\b\s*[-\u2013\u2014:,]?\s*(.*)$", re.I)

    for line in cleaned.split("\n"):
        line = line.strip().lstrip("-*\u2022 ").strip()
        if not line:
            continue
        m = pattern.match(line)
        if m:
            idx = int(m.group(1))
            if 1 <= idx <= count and idx not in by_index:
                by_index[idx] = {
                    "approved": m.group(2).upper() == "YES",
                    "reason": (m.group(3) or "").strip() or "no reason given",
                }

    if not by_index:
        return [{"approved": True, "reason": "Verdicts unreadable; unfiltered"}
                for _ in range(count)]

    return [by_index.get(i, {"approved": True, "reason": "No verdict; kept"})
            for i in range(1, count + 1)]


def judge_give_candidates(article, candidates):
    if not candidates:
        return candidates
    response = _call_gemini(_build_give_prompt(article, candidates))
    for cand, j in zip(candidates, _parse_judgments(response, len(candidates))):
        cand["gemini_approved"] = j["approved"]
        cand["gemini_reason"] = j["reason"]
    return candidates


def judge_receive_candidates(article, candidates):
    if not candidates:
        return candidates
    response = _call_gemini(_build_receive_prompt(article, candidates))
    for cand, j in zip(candidates, _parse_judgments(response, len(candidates))):
        cand["gemini_approved"] = j["approved"]
        cand["gemini_reason"] = j["reason"]
    return candidates


# ---------------------------------------------------------------- link generation

def _build_link_prompt(paragraphs, target, feedback=""):
    target_title = target.get("h1") or target.get("title_clean") or ""
    target_url = target["url"]
    target_section = target.get("section", "")
    para_lines = [f'[{p["index"]}] {p["text"]}' for p in paragraphs]
    feedback_block = ""
    if feedback:
        feedback_block = (f"\nYOUR PREVIOUS ATTEMPT WAS NOT USABLE: {feedback}\n"
                          "Fix exactly that problem and return a corrected version. "
                          "Do not give up; a good placement almost certainly exists "
                          "in these paragraphs.\n")

    return f"""You are placing ONE internal link on a cancer-education website (binaytara.org). Your goal is to PRODUCE a natural, usable link, not to find reasons to refuse. A human editor should be able to paste your result with no further thought.

LINK TARGET (the page the link points to):
  Title: {target_title}
  URL: {target_url}
  Section: {target_section}
{feedback_block}
SOURCE PARAGRAPHS (from the article being edited; pick exactly ONE):
{chr(10).join(para_lines)}

HOW TO CHOOSE THE ANCHOR (the clickable words):
- It must be a 2-to-5-word TOPIC phrase: a disease, drug, treatment, or concept that describes what the reader will find on the target page.
- It must read naturally as link text on its own. "breast cancer", "KRAS inhibitors", "checkpoint inhibitor toxicity" are good. A person's name with credentials ("Rosa Nadal Rios MD PhD"), an institution or event name ("MD Anderson", "GU Cancers Summit"), or a mid-sentence fragment ("on Advances", "First Positive", "Treatment Diversifies") are NOT acceptable anchors.
- Strongly prefer a phrase that already appears word-for-word in the paragraph. If a perfect phrase is not present, you MAY make a small, natural edit to the sentence to introduce one (add a few words), as long as you add NO new medical facts, numbers, or claims.

WHERE NOT TO PLACE IT:
- Not in a sentence that lists 3 or more different cancer types (a statistics list).
- Not in a sentence that is a person's biography (name + credentials + "is a...").

OUTPUT: return ONLY this JSON object, no markdown fences, no commentary:
{{"found": true, "paragraph_index": <int from the list above>, "anchor": "<2-5 word topic phrase>", "existing_sentence": "<the exact original sentence you chose>", "modified_sentence": "<that same sentence with the anchor wrapped as [anchor]({target_url})>"}}

Only if NONE of these paragraphs share any real topic with the target page (a genuine mismatch, not just an imperfect anchor), return:
{{"found": false, "reason": "<one short sentence>"}}

Choosing "found": false when a workable link exists is a failure. Choose it only for a true topic mismatch."""


def _parse_link_json(response):
    if not response:
        return None
    text = response.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def generate_link(paragraphs, target, max_attempts=3):
    """Ask Gemini to pick placement, anchor, and rewritten sentence in one
    call; validate; retry with specific feedback up to max_attempts; drop
    (return None) if it never passes. Replaces v6's code-only anchor
    slicing, which produced ungrammatical fragment anchors."""
    if not paragraphs:
        return None
    if not _get_api_keys():
        return None

    feedback = ""
    for _attempt in range(max_attempts):
        prompt = _build_link_prompt(paragraphs, target, feedback)
        response = _call_gemini(prompt, max_tokens=500, temperature=0.2)
        parsed = _parse_link_json(response)

        if parsed is None:
            feedback = "Your response was not valid JSON. Return ONLY the JSON object."
            continue
        if not parsed.get("found", False):
            return None

        anchor = parsed.get("anchor", "")
        existing = parsed.get("existing_sentence", "")
        modified = parsed.get("modified_sentence", "")

        ok, reason = lv.validate_generated_link(anchor, target["url"], existing, modified)
        if ok:
            return {
                "anchor": anchor,
                "paragraph_index": parsed.get("paragraph_index"),
                "existing_sentence": existing,
                "modified_sentence": modified,
            }
        feedback = reason

    return None
