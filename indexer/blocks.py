"""Block classification: decides which parts of an article may receive a link.

This module is shared by the HTML extractor and the .docx parser, so the SOP
skip rules (first paragraph, Key Takeaways, references, quotes) apply identically
to published pages and to unpublished drafts.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from config import selectors as sel
from config import settings

HEADING_KINDS = {"h2", "h3", "h4", "h5", "h6"}

_key_re = re.compile(sel.KEY_TAKEAWAYS_RE, re.I)
_ref_re = re.compile(sel.REFERENCES_RE, re.I)
_cap_re = re.compile(sel.CAPTION_RE, re.I)
_dis_re = re.compile(sel.DISCLAIMER_RE, re.I)

# v5b: cancer-type terms for statistics-list detection. Multi-word terms first
# so "breast cancer" matches before a bare "breast."
_CANCER_TYPES_FOR_LIST = [
    "renal cell carcinoma", "hepatocellular carcinoma", "colorectal cancer",
    "non-small cell lung cancer", "small cell lung cancer", "head and neck cancer",
    "multiple myeloma", "urothelial carcinoma", "neuroendocrine tumor",
    "squamous cell carcinoma", "biliary tract cancer", "triple negative breast cancer",
    "esophageal cancer", "pancreatic cancer", "prostate cancer", "cervical cancer",
    "ovarian cancer", "stomach cancer", "gastric cancer", "kidney cancer",
    "bladder cancer", "breast cancer", "lung cancer", "liver cancer",
    "thyroid cancer", "brain cancer", "skin cancer", "oral cancer",
    "colon cancer", "rectal cancer", "testicular cancer", "bone cancer",
    "blood cancer", "endometrial cancer", "uterine cancer", "vulvar cancer",
    "anal cancer", "tongue cancer", "throat cancer", "eye cancer",
    "gallbladder cancer", "bile duct cancer", "adrenal cancer",
    "glioblastoma", "melanoma", "lymphoma", "leukemia", "leukaemia",
    "myeloma", "sarcoma", "mesothelioma", "liposarcoma",
]


def _count_cancer_types(text: str) -> int:
    """Count how many distinct cancer types a paragraph mentions."""
    lower = (text or "").lower()
    found: set[str] = set()
    for term in _CANCER_TYPES_FOR_LIST:
        if term in lower:
            # Avoid double-counting: "breast cancer" should not also count the
            # shorter "breast" if we add more terms later. Use the full term as
            # the dedup key.
            base = term.replace(" cancer", "").replace(" carcinoma", "")
            if base not in found:
                found.add(base)
    return len(found)
_attr_re = re.compile(sel.ATTRIBUTION_RE, re.I)
_cred_re = re.compile(sel.CREDENTIAL_RE)


@dataclass
class Block:
    """One paragraph, heading, list or quote from an article body."""
    index: int
    kind: str                      # p | h2..h6 | ul | ol | blockquote | caption
    text: str
    links: list = field(default_factory=list)   # [(url, anchor_text)]
    style: str | None = None       # Word style name, for .docx input
    eligible: bool = False
    skip_reason: str | None = None
    heading_context: str = ""

    @property
    def word_count(self) -> int:
        return len(self.text.split())


def _heading_level(kind: str) -> int:
    return int(kind[1]) if kind in HEADING_KINDS else 99


def _strip_apostrophes(text: str) -> str:
    """Remove curly/straight apostrophes that sit between letters, so that
    "cancer\u2019s" is not mistaken for an opening quotation mark."""
    return re.sub(r"(?<=[A-Za-z])[\u2019\u2018'](?=[A-Za-z])", "", text or "")


def _quote_char_ratio(text: str) -> float:
    """Fraction of characters sitting inside paired quotation marks."""
    if not text:
        return 0.0
    inside, open_q, count = False, None, 0
    for ch in text:
        if ch in sel.STRONG_QUOTE_CHARS:
            if not inside:
                inside, open_q = True, ch
            else:
                inside, open_q = False, None
            continue
        if inside:
            count += 1
    return count / len(text)


def _jaccard(a: str, b: str) -> float:
    ta = {w for w in re.findall(r"[a-z0-9]+", (a or "").lower()) if len(w) > 2}
    tb = {w for w in re.findall(r"[a-z0-9]+", (b or "").lower()) if len(w) > 2}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def is_quote(block: Block) -> bool:
    """Bias deliberately toward over-detecting. A missed suggestion is cheap; a
    writer editing a physician's attributed quote is not."""
    if block.kind == "blockquote":
        return True
    if block.style and "quote" in block.style.lower():
        return True
    text = _strip_apostrophes(block.text)
    has_quote_mark = any(c in text for c in sel.STRONG_QUOTE_CHARS)
    if not has_quote_mark:
        return False
    if _quote_char_ratio(text) > 0.40:
        return True
    # Attribution verb within 60 characters of a quotation mark.
    for m in re.finditer(f"[{re.escape(sel.STRONG_QUOTE_CHARS)}]", text):
        window = text[max(0, m.start() - 60): m.start() + 60]
        if _attr_re.search(window):
            return True
    # Credential pattern, but only alongside a quotation mark.
    if _cred_re.search(text):
        return True
    return False


def classify(blocks: list[Block], meta_description: str = "") -> list[Block]:
    """Annotate each block with eligible / skip_reason. Mutates and returns."""
    # Pass 1: heading-driven regions (Key Takeaways, References).
    in_key, key_level = False, 99
    in_refs = False
    for b in blocks:
        lvl = _heading_level(b.kind)
        if in_refs:
            b.skip_reason = "REFERENCES"
            continue
        if b.kind in HEADING_KINDS and _ref_re.search(b.text):
            in_refs = True
            b.skip_reason = "REFERENCES"
            continue
        if in_key:
            if b.kind in HEADING_KINDS and lvl <= key_level:
                in_key = False          # region ended; fall through to normal rules
            else:
                b.skip_reason = "KEY_TAKEAWAYS"
                continue
        if b.kind in HEADING_KINDS and _key_re.search(b.text):
            in_key, key_level = True, lvl
            b.skip_reason = "KEY_TAKEAWAYS"
            continue

    # Pass 2: per-block rules.
    first_marked = False
    for b in blocks:
        if b.skip_reason:
            continue
        if b.kind in HEADING_KINDS:
            b.skip_reason = "HEADING"
            continue
        if b.style and b.style.lower().startswith("caption"):
            b.skip_reason = "CAPTION"
            continue
        if _cap_re.search(b.text):
            b.skip_reason = "CAPTION"
            continue
        if _dis_re.search(b.text):
            b.skip_reason = "DISCLAIMER"
            continue
        if is_quote(b):
            b.skip_reason = "QUOTE"
            continue
        if any("/cancernews/contributors/" in (u or "") for u, _ in b.links) \
                and re.match(r"^\s*(author|by)\b", b.text, re.I):
            b.skip_reason = "AUTHOR_BLOCK"
            continue
        if b.word_count < settings.MIN_BLOCK_WORDS:
            b.skip_reason = "SHORT"
            continue
        # v5b: Statistics-list paragraphs. A paragraph mentioning 4+ distinct
        # cancer types is a data list (e.g., "17% for breast cancer, 10% for
        # colorectal and prostate cancer, 30% for biliary tract cancer...").
        # Linking one disease in a list of many is misleading: it implies the
        # destination has those specific statistics. Skip entirely.
        if _count_cancer_types(b.text) >= 4:
            b.skip_reason = "STATISTICS_LIST"
            continue
        # First substantive paragraph (SOP Rule 1). Guard 1 handles a short dek,
        # which is already filtered by the SHORT rule above. Guard 2 catches a
        # lede that restates the meta description.
        if not first_marked and b.kind in ("p", "ul", "ol"):
            b.skip_reason = "FIRST_PARAGRAPH"
            first_marked = True
            continue
        if meta_description and b.kind == "p" and \
                _jaccard(b.text, meta_description) > 0.70:
            b.skip_reason = "FIRST_PARAGRAPH"
            continue
        b.eligible = True

    # Pass 3: heading context carried down to following blocks.
    ctx = ""
    for b in blocks:
        if b.kind in HEADING_KINDS:
            ctx = b.text.strip()
        else:
            b.heading_context = ctx
    return blocks
