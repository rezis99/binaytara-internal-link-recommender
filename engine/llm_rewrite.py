"""LLM sentence rewriting for 'Needs insertion' rows (v3).

Writers need a complete proposed sentence they can approve or reject, not an
editing assignment like 'incorporate the phrase X naturally'. This module takes
the original sentence and the required anchor phrase, and returns one rewritten
sentence with the anchor woven in.

Validation is strict:
  - The anchor phrase must appear VERBATIM in the rewritten sentence.
  - No new medical claims, changed numbers, or altered drug names.
  - Sentence length must stay within 0.5x to 2.0x of the original.
  - If any validation fails, the original manual instruction is returned.

Providers (checked in order):
  1. Groq free tier (GROQ_API_KEY in env or Streamlit secrets)
  2. HuggingFace Inference API (HF_TOKEN in env or Streamlit secrets)
  3. Disabled: returns the manual instruction unchanged.
"""
from __future__ import annotations

import json
import os
import re
from functools import lru_cache

import httpx

from config import settings

SYSTEM_PROMPT = (
    "You rewrite sentences for a cancer education website. You will be given an "
    "original sentence and an anchor phrase. Return ONLY the rewritten sentence "
    "with the anchor phrase woven in naturally. Rules:\n"
    "1. The anchor phrase must appear VERBATIM (exact words, exact spelling).\n"
    "2. Do NOT add medical claims, statistics, drug names, or facts not in the original.\n"
    "3. Keep the original meaning and tone.\n"
    "4. Keep similar length (no more than twice the original).\n"
    "5. Return ONLY the rewritten sentence, nothing else. No quotes, no explanation."
)


def _user_prompt(sentence: str, anchor: str, target_title: str) -> str:
    return (
        f"Original sentence: {sentence}\n"
        f"Anchor phrase to include: {anchor}\n"
        f"The anchor will link to a page titled: {target_title}\n"
        f"Rewritten sentence:"
    )


@lru_cache(maxsize=1)
def _get_api_key(provider: str) -> str | None:
    """Check environment, then Streamlit secrets."""
    if provider == "groq":
        key = os.environ.get("GROQ_API_KEY")
        if key:
            return key
        try:
            import streamlit as st
            return st.secrets.get("GROQ_API_KEY")
        except Exception:
            return None
    elif provider == "huggingface":
        key = os.environ.get("HF_TOKEN")
        if key:
            return key
        try:
            import streamlit as st
            return st.secrets.get("HF_TOKEN")
        except Exception:
            return None
    return None


def _call_groq(sentence: str, anchor: str, target_title: str) -> str | None:
    key = _get_api_key("groq")
    if not key:
        return None
    try:
        r = httpx.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": settings.LLM_MODEL_GROQ,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": _user_prompt(sentence, anchor, target_title)},
                ],
                "temperature": 0.3,
                "max_tokens": 300,
            },
            timeout=settings.LLM_TIMEOUT,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        return (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
    except Exception:
        return None


def _call_huggingface(sentence: str, anchor: str, target_title: str) -> str | None:
    key = _get_api_key("huggingface")
    if not key:
        return None
    try:
        prompt = f"{SYSTEM_PROMPT}\n\n{_user_prompt(sentence, anchor, target_title)}"
        r = httpx.post(
            f"https://api-inference.huggingface.co/models/{settings.LLM_MODEL_HF}",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"inputs": prompt, "parameters": {"max_new_tokens": 300, "temperature": 0.3}},
            timeout=settings.LLM_TIMEOUT,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if isinstance(data, list) and data:
            text = data[0].get("generated_text", "")
            # HF returns the full prompt + completion; extract just the completion.
            if "Rewritten sentence:" in text:
                text = text.split("Rewritten sentence:")[-1].strip()
            return text.strip()
        return None
    except Exception:
        return None


def _validate(original: str, rewrite: str, anchor: str) -> bool:
    """Strict validation. Any failure means the rewrite is discarded."""
    if not rewrite:
        return False
    # Anchor must appear verbatim (case-insensitive word-boundary match).
    if not re.search(r"\b" + re.escape(anchor) + r"\b", rewrite, re.I):
        return False
    # Length check: rewrite should be 0.5x to 2.5x the original.
    orig_words = len(original.split())
    new_words = len(rewrite.split())
    if orig_words > 0 and (new_words < orig_words * 0.5 or new_words > orig_words * 2.5):
        return False
    # No markdown, HTML, or meta-commentary.
    if any(marker in rewrite for marker in ["[", "]", "<", ">", "```", "Here is", "Note:"]):
        return False
    return True


def rewrite(sentence: str, anchor: str, target_title: str,
            target_url: str) -> tuple[str, bool]:
    """Attempt an LLM rewrite. Returns (modified_sentence, was_rewritten).

    If the LLM is unavailable or validation fails, returns the manual
    instruction from v2 and was_rewritten=False.
    """
    if not settings.LLM_ENABLED:
        return _fallback(sentence, anchor, target_url), False

    result = None
    for attempt in range(settings.LLM_MAX_RETRIES):
        # Try Groq first, then HuggingFace.
        if settings.LLM_PROVIDER in ("groq", "auto"):
            result = _call_groq(sentence, anchor, target_title)
        if result is None and settings.LLM_PROVIDER in ("huggingface", "auto"):
            result = _call_huggingface(sentence, anchor, target_title)
        if result and _validate(sentence, result, anchor):
            # Build the markdown link version of the rewrite.
            # Find the anchor in the rewrite and wrap it.
            m = re.search(r"\b" + re.escape(anchor) + r"\b", result, re.I)
            if m:
                linked = (f"{result[:m.start()]}[{result[m.start():m.end()]}]"
                          f"({target_url}){result[m.end():]}")
                return linked, True
            return result, True
        result = None  # retry

    return _fallback(sentence, anchor, target_url), False


def _fallback(sentence: str, anchor: str, target_url: str) -> str:
    """The v2 manual instruction, used when LLM is unavailable."""
    from engine.rules import display_url
    shown = display_url(target_url)
    return (f'Writer to incorporate the phrase "{anchor}" naturally into this '
            f"sentence, then link it to {shown}")


def is_available() -> tuple[bool, str]:
    """Check whether any LLM provider is configured. Returns (available, provider_name)."""
    if not settings.LLM_ENABLED:
        return False, "disabled in settings"
    if _get_api_key("groq"):
        return True, "Groq"
    if _get_api_key("huggingface"):
        return True, "HuggingFace"
    return False, "no API key configured"
