"""Validation for LLM-generated anchor/sentence pairs (v7).

Every rule here exists because a real bad row showed up in manual QA of the
v6 output. Each function's docstring names the QA finding it fixes.

This module has zero dependency on the Gemini client so it can be unit
tested without any API calls, and so the same checks apply regardless of
which model produced the candidate.
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------- anchors

# QA finding #5, #14: words that make an anchor a fragment or a person's
# name/credential block rather than a topic. An anchor should never START
# or END on one of these, and should never CONTAIN a credential marker.
_BAD_EDGE_WORDS = {
    "a", "an", "the", "on", "for", "and", "or", "with", "vs", "of", "in",
    "from", "to", "at", "is", "are", "dr", "first", "second", "third",
    "new", "part", "i", "ii", "iii", "one", "two", "three", "four", "five",
    "her", "his", "their", "our", "your", "this", "that", "these", "those",
    "low", "high", "early", "late", "old", "big", "small", "major", "minor",
    "related", "common", "rare", "over", "under", "into", "onto", "than",
}
_CREDENTIAL_RE = re.compile(
    r"\b(MD|PhD|MBBS|DO|MSCI|FACP|DNB|RN|MPH)\b", re.I)
_INSTITUTION_RE = re.compile(
    r"\b(Summit|Conference|Center|Centre|Anderson|Hospital|Institute|"
    r"University|Foundation|Symposium|Assessment|Initiative|Program)\b", re.I)


def is_low_quality_anchor(anchor: str) -> tuple[bool, str]:
    """QA #5, #14, #7 (part): reject anchors that cannot read as natural
    link text, independent of what page they'd point to.

    Real failures this catches: "Dr Gentry King on Advances", "First
    Positive", "Rosa Nadal Rios MD PhD", "Community Health Needs
    Assessment", "Atlanta Genitourinary Cancers Summit", "MD Anderson".
    """
    anchor = (anchor or "").strip()
    words = anchor.split()
    if len(words) < 2 or len(words) > 5:
        return True, f"anchor is {len(words)} words, must be 2 to 5"

    first, last = words[0].lower().strip(",."), words[-1].lower().strip(",.")
    if first in _BAD_EDGE_WORDS:
        return True, f"anchor starts with a function word ('{words[0]}')"
    if last in _BAD_EDGE_WORDS:
        return True, f"anchor ends with a function word ('{words[-1]}')"
    if _CREDENTIAL_RE.search(anchor):
        return True, "anchor contains a person's credentials, not a topic phrase"
    if _INSTITUTION_RE.search(anchor):
        return True, "anchor names an institution or event, not a topic"
    return False, ""


# Generic anchor blocklist. QA #7 found "overall survival", "newly
# diagnosed", "bone marrow" slipping through the v6 list.
GENERIC_ANCHORS = {
    "cancer risk", "risk factors", "all cancer", "all cancers",
    "early detection", "clinical trials", "treatment options",
    "new study", "new research", "cancer awareness", "the disease",
    "overall survival", "newly diagnosed", "bone marrow",
    "progression free survival", "median survival", "response rate",
    "adverse events", "quality of life", "standard of care",
}


def is_generic_anchor(anchor: str) -> bool:
    return (anchor or "").strip().lower() in GENERIC_ANCHORS


# Words that are too generic to prove an anchor matches a destination's topic.
_TOPIC_STOPWORDS = {
    "and", "or", "the", "a", "an", "of", "in", "for", "with", "to", "on",
    "at", "is", "are", "cancer", "cancers", "disease", "care", "treatment",
    "new", "health", "patient", "patients", "risk", "study", "research",
    "awareness", "month", "day", "guide", "overview", "understanding",
}


def anchor_matches_destination(anchor: str, target_url: str,
                               target_title: str) -> tuple[bool, str]:
    """QA (mammogram bug): the anchor must describe the DESTINATION page's
    topic. "breast cancer" pointing at a mammogram-screening-myths page is
    wrong even though both are women's-health topics.

    Proof of a match = at least one meaningful (non-stopword, non-generic)
    word shared between the anchor and EITHER the destination's URL slug OR
    its title. The slug is checked because it is the most honest one-line
    summary of what a page is about, exactly as the reviewer suggested.
    """
    anchor_words = {w for w in re.findall(r"[a-z]+", (anchor or "").lower())
                    if w not in _TOPIC_STOPWORDS and len(w) > 2}
    if not anchor_words:
        return False, f"anchor '{anchor}' has no topical word to match the destination"

    slug = (target_url or "").rstrip("/").split("/")[-1]
    slug_words = {w for w in re.findall(r"[a-z]+", slug.lower())
                  if w not in _TOPIC_STOPWORDS and len(w) > 2}
    title_words = {w for w in re.findall(r"[a-z]+", (target_title or "").lower())
                   if w not in _TOPIC_STOPWORDS and len(w) > 2}

    dest_words = slug_words | title_words
    if anchor_words & dest_words:
        return True, ""
    return False, (f"anchor '{anchor}' does not match the destination page's "
                   f"topic (slug: {slug[:40]}); pick a phrase that reflects what "
                   "that page is actually about")


# ---------------------------------------------------------------- paragraphs

# QA #9: bio paragraphs used as link-insertion targets. A contributor bio
# ("Fengting Yan, MD, PhD, FACP, clinical oncologist...is a...") is not
# topical content and should never carry a topical link.
_BIO_PATTERNS = [
    re.compile(r"\b(MD|PhD|MBBS|DO)\b.{0,80}\bis an?\b", re.I),
    re.compile(r"\bpractices at\b", re.I),
    re.compile(r"\b(her|his|their)\s+work\s+focuses\s+on\b", re.I),
    re.compile(r"\bis an?\s+(assistant|associate|clinical|medical)\s+"
              r"(professor|oncologist)\b", re.I),
    re.compile(r"\bpublished articles\s*\(\d+\)", re.I),
    # "Fengting Yan, MD, PhD, FACP, clinical oncologist, weighed in..."
    # A sentence opening with Name, credential(s) is a bio lead-in even
    # without "is a" or "practices at" appearing anywhere in it.
    re.compile(r"^[A-Z][a-zA-Z.'-]+\s+[A-Z][a-zA-Z.'-]+,\s*"
              r"(MD|PhD|MBBS|DO)\b", re.I),
]


def is_bio_paragraph(text: str) -> bool:
    return any(p.search(text or "") for p in _BIO_PATTERNS)


# QA #10: statistics-list detection must run PER SENTENCE, not per
# paragraph, because a non-list paragraph can still contain one list
# sentence, and a link placed in that one sentence is still wrong even
# though the paragraph as a whole is fine.
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")

_CANCER_TERMS = [
    "renal cell carcinoma", "hepatocellular carcinoma", "colorectal cancer",
    "non-small cell lung cancer", "small cell lung cancer",
    "head and neck cancer", "multiple myeloma", "urothelial carcinoma",
    "neuroendocrine tumor", "squamous cell carcinoma", "biliary tract cancer",
    "triple negative breast cancer", "esophageal cancer", "oesophageal cancer",
    "pancreatic cancer", "prostate cancer", "cervical cancer", "ovarian cancer",
    "stomach cancer", "gastric cancer", "kidney cancer", "bladder cancer",
    "breast cancer", "lung cancer", "liver cancer", "thyroid cancer",
    "brain cancer", "skin cancer", "oral cancer", "colon cancer",
    "rectal cancer", "testicular cancer", "bone cancer", "blood cancer",
    "endometrial cancer", "uterine cancer", "glioblastoma", "melanoma",
    "lymphoma", "leukemia", "leukaemia", "myeloma", "sarcoma", "mesothelioma",
]


def sentence_cancer_type_count(sentence: str) -> int:
    lower = (sentence or "").lower()
    found: set[str] = set()
    for term in _CANCER_TERMS:
        if term in lower:
            base = term.replace(" cancer", "").replace(" carcinoma", "")
            if not any(base in f for f in found):
                found.add(base)
    return len(found)


def sentence_is_statistics_list(sentence: str) -> bool:
    """True when a SINGLE sentence enumerates 3+ cancer types.

    Three independent detection paths, because real statistics sentences
    name cancers in different grammatical shapes:
      1. Each item repeats the word "cancer": "breast cancer, kidney
         cancer, and lung cancer".
      2. Elliptical coordination: "cancers of the skin (melanoma),
         stomach, pancreas, gallbladder, and prostate" — "cancer" appears
         once, the list items are bare organ/site names.
      3. Statistic-dense listing: "17% for breast cancer, 10% for
         colorectal and prostate cancer, and 30% for biliary tract
         cancer" — coordination drops "cancer" from some items
         ("colorectal and prostate cancer" = colorectal cancer AND
         prostate cancer), so path 1 undercounts; the presence of
         multiple percentages alongside multiple commas is the signal.
    """
    lower = (sentence or "").lower()

    if sentence_cancer_type_count(sentence) >= 3:
        return True

    m = re.search(r"cancers? of (?:the )?([a-z()\s]+(?:,\s*[a-z()\s]+){2,})",
                  lower)
    if m:
        items = re.split(r",\s*|\s+and\s+", m.group(1))
        items = [i.strip() for i in items if i.strip()]
        if len(items) >= 3:
            return True

    cancer_mentions = lower.count("cancer")
    has_stats = bool(re.search(r"\d+%|\d+\s*percent", lower))
    comma_count = lower.count(",")
    if cancer_mentions >= 2 and has_stats and comma_count >= 2:
        return True

    return False


def find_eligible_sentence(paragraph_text: str) -> list[str]:
    """Split a paragraph into sentences and drop the ones a link must
    never land in: statistics lists and (as a second line of defence)
    bio-shaped sentences. Returns the sentences still eligible."""
    sentences = _SENT_SPLIT.split(paragraph_text or "")
    return [s for s in sentences
            if s.strip() and not sentence_is_statistics_list(s)
            and not is_bio_paragraph(s)]


# ---------------------------------------------------------------- full check

def validate_generated_link(anchor: str, target_url: str, existing_sentence: str,
                            modified_sentence: str, target_title: str = "") -> tuple[bool, str]:
    """The single gate every LLM-generated (anchor, sentence) pair must
    clear before it can reach a writer. Combines every rule above plus a
    minimal-change check so the model cannot rewrite past the anchor
    insertion (QA #8: 'Modified Sentence is not a modified sentence', and
    the sugar-drink paragraph concern: no new facts get introduced).
    """
    anchor = (anchor or "").strip()
    if not anchor:
        return False, "no anchor returned"

    bad, why = is_low_quality_anchor(anchor)
    if bad:
        return False, why
    if is_generic_anchor(anchor):
        return False, f"'{anchor}' is a generic anchor, not a topic"

    # The anchor must describe the destination page (mammogram bug fix).
    matches, mreason = anchor_matches_destination(anchor, target_url, target_title)
    if not matches:
        return False, mreason

    if not modified_sentence or not existing_sentence:
        return False, "missing sentence"

    link_pattern = f"[{anchor}]({target_url})"
    if link_pattern not in modified_sentence:
        # Allow case-insensitive anchor match (model may alter case slightly)
        m = re.search(re.escape(f"](") + re.escape(target_url) + r"\)",
                      modified_sentence)
        if not m or f"[{anchor}" not in modified_sentence:
            return False, "modified sentence does not contain [anchor](url) exactly"

    if modified_sentence.strip() == existing_sentence.strip():
        return False, "modified sentence identical to original; anchor never inserted"

    if sentence_is_statistics_list(existing_sentence):
        return False, "source sentence lists 3+ cancer types (statistics list)"
    if is_bio_paragraph(existing_sentence):
        return False, "source sentence is a contributor bio, not topical content"

    # Change-scope check. The new sentence may legitimately add a few words
    # to introduce the anchor naturally (the prompt allows this), so we do
    # NOT cap added words tightly. What we DO block is fabricated evidence:
    # a number, percentage, or statistic that was not in the original. That
    # is the one edit that makes a suggestion unsafe for a medical site.
    plain = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", modified_sentence)

    orig_nums = set(re.findall(r"\d[\d,.]*%?", existing_sentence))
    new_nums = set(re.findall(r"\d[\d,.]*%?", plain))
    if new_nums - orig_nums:
        return False, (f"rewrite introduced a number/statistic not in the "
                       f"original ({sorted(new_nums - orig_nums)}); no new facts allowed")

    orig_words = set(w.lower() for w in existing_sentence.split())
    new_words = set(w.lower() for w in plain.split())
    removed = orig_words - new_words
    if len(removed) > 3:
        return False, (f"too many original words dropped ({len(removed)}); "
                       "keep the sentence's meaning intact")

    return True, "ok"
