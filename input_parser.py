"""Article input: a live URL or an uploaded .docx draft.

The .docx path cannot reuse the HTML block rules unchanged, because several of
them depend on elements Word does not have (<blockquote>, <figure> adjacency,
anchor elements). The mapping below is explicit.
"""
from __future__ import annotations

import io
import ipaddress
import re
import socket
from urllib.parse import urlparse

import httpx

from config import settings
from config import url_rules as ur
from indexer.blocks import Block, classify
from indexer.chunker import chunk_page
from indexer.extractor import extract

PRIVATE_NETS = [
    ipaddress.ip_network(n) for n in
    ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8",
     "169.254.0.0/16", "0.0.0.0/8", "::1/128", "fc00::/7", "fe80::/10")
]


class InputError(Exception):
    pass


def _assert_safe_url(url: str) -> str:
    """SSRF controls. The host allowlist is the strongest one and reduces most of
    the rest to defence in depth; they are implemented anyway because the
    allowlist is one careless edit away from being loosened."""
    # Check the RAW scheme before normalising. normalise() force-upgrades the
    # scheme to https, which is the right behaviour for links discovered on a
    # page but wrong for user input: it would silently accept http:// and any
    # other scheme that happens to parse, instead of refusing it.
    raw_scheme = (urlparse((url or "").strip()).scheme or "").lower()
    if raw_scheme and raw_scheme != "https":
        raise InputError("Only https URLs are accepted.")
    n = ur.normalise(url)
    if not n:
        raise InputError("That does not look like a valid URL.")
    p = urlparse(n)
    if p.scheme != "https":
        raise InputError("Only https URLs are accepted.")
    if p.netloc != settings.ALLOWED_HOST:
        raise InputError(
            f"Only {settings.ALLOWED_HOST} pages can be analysed. "
            "Subdomains are out of scope."
        )
    try:
        for info in socket.getaddrinfo(p.netloc, 443):
            ip = ipaddress.ip_address(info[4][0])
            if any(ip in net for net in PRIVATE_NETS):
                raise InputError("Refusing to fetch a private network address.")
    except socket.gaierror:
        raise InputError("Could not resolve that host.") from None
    return n


def from_url(url: str) -> dict:
    safe = _assert_safe_url(url)
    headers = {"User-Agent": settings.USER_AGENT}
    with httpx.Client(headers=headers, follow_redirects=True,
                      timeout=settings.REQUEST_TIMEOUT,
                      max_redirects=settings.MAX_REDIRECTS) as client:
        r = client.get(safe)
        for hop in list(r.history) + [r]:
            # Re-apply the allowlist to EVERY hop: a permitted host redirecting
            # to an internal address is the classic bypass.
            _assert_safe_url(str(hop.url))
        if r.status_code != 200:
            raise InputError(f"That page returned HTTP {r.status_code}.")
        if "text/html" not in r.headers.get("content-type", ""):
            raise InputError("That URL did not return an HTML page.")
        if len(r.content) > settings.MAX_RESPONSE_BYTES:
            raise InputError("That page is too large to analyse.")
        final = ur.normalise(str(r.url)) or safe
        rec = extract(r.text, final)
    if rec is None:
        raise InputError("No article body could be found on that page.")
    blocks = rec.pop("_blocks")
    rec["chunks"] = chunk_page(rec["url"], blocks)
    rec["blocks"] = blocks
    rec["is_draft"] = False
    return rec


_HEADING_STYLE = re.compile(r"^heading\s*([1-6])$", re.I)


def _docx_links(paragraph) -> list[tuple[str, str]]:
    """Word stores hyperlink targets in the relationship part, not inline. A
    naive read of paragraph text finds none of them, which silently breaks R7 on
    drafts."""
    out = []
    try:
        rels = paragraph.part.rels
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
              "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
        for link in paragraph._p.findall(".//w:hyperlink", ns):
            rid = link.get(
                "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
            if not rid or rid not in rels:
                continue
            target = rels[rid].target_ref
            text = "".join(t.text or "" for t in link.findall(".//w:t", ns))
            out.append((target, text))
    except Exception:                                          # noqa: BLE001
        return []
    return out


def from_docx(data: bytes, filename: str = "draft.docx") -> dict:
    if len(data) > settings.MAX_UPLOAD_BYTES:
        raise InputError("That file is larger than the 10 MB limit.")
    if data[:2] != b"PK":
        raise InputError("That does not look like a .docx file.")

    import docx                                                # local import

    doc = docx.Document(io.BytesIO(data))

    blocks, idx, h1 = [], 0, ""
    for para in doc.paragraphs:
        text = re.sub(r"\s+", " ", para.text or "").strip()
        if not text:
            continue
        style = (para.style.name if para.style is not None else "") or ""
        m = _HEADING_STYLE.match(style.strip())
        if m:
            level = int(m.group(1))
            if level == 1 and not h1:
                h1 = text
                idx += 1
                continue
            kind = f"h{min(max(level, 2), 6)}"
        elif style.lower().startswith("caption"):
            kind = "p"
        elif "quote" in style.lower():
            kind = "blockquote"
        elif style.lower().startswith("list"):
            kind = "ul"
        else:
            kind = "p"
        blocks.append(Block(index=idx, kind=kind, text=text, style=style,
                            links=_docx_links(para)))
        idx += 1

    if not blocks:
        raise InputError("No readable paragraphs were found in that document.")

    classify(blocks)
    body_links, seen = [], set()
    for b in blocks:
        for u, _a in b.links:
            n = ur.normalise(u)
            if n and ur.is_allowed(n) and n not in seen:
                seen.add(n)
                body_links.append(n)

    body_text = " ".join(b.text for b in blocks)
    url = f"draft://{filename}"
    rec = {
        "url": url, "section": "Draft", "title": h1 or filename,
        "title_clean": h1 or filename, "h1": h1 or "",
        "meta_description": "", "meta_description_is_placeholder": False,
        "abstract": "", "abstract_missing": False,
        "word_count": len(body_text.split()),
        "body_internal_links": body_links, "nav_internal_links": [],
        "event": None, "person_names": [], "inbound_link_count": 0,
        "body_available": True, "is_draft": True,
        "blocks": blocks,
        "chunks": chunk_page(url, [
            {"index": b.index, "kind": b.kind, "text": b.text,
             "eligible": b.eligible, "skip_reason": b.skip_reason,
             "heading_context": b.heading_context} for b in blocks
        ]),
    }
    return rec
