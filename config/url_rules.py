"""URL filtering and normalisation. Decides what may enter the index at all."""
import re
from urllib.parse import urlparse, urlunparse

from config import settings

# Hosts that are never indexed and never suggested.
BLOCKED_HOST_SUBSTRINGS = ("binayfoundation.org",)

# Paths excluded entirely (regex, matched against the path).
EXCLUDE_PATH_PATTERNS = [
    r"^/cancernews/contributors/?$",      # the listing page itself
    r"^/privacy-policy",
    r"^/careers",
    r"\.(pdf|xml|json|jpg|jpeg|png|webp|gif|svg|zip|docx?|xlsx?)$",
    r"/download\.pdf$",
]

# Contributor pages are valid targets ONLY under this prefix.
CONTRIBUTOR_PREFIX = "/cancernews/contributors/"

SECTION_RULES = [
    (CONTRIBUTOR_PREFIX, "Contributor"),
    ("/cancernews/", "TCN"),
    ("/journal/", "IJCCD"),
    ("/news/", "Blog"),
    ("/projects/conferences", "Conference"),
    ("/projects/", "Project"),
]

_exclude_re = [re.compile(p, re.I) for p in EXCLUDE_PATH_PATTERNS]


def normalise(url: str) -> str | None:
    """Force https, drop query and fragment, strip trailing slash. None if unusable."""
    if not url:
        return None
    url = url.strip()
    try:
        p = urlparse(url)
    except ValueError:
        return None
    if not p.netloc:
        return None
    host = p.netloc.lower().split(":")[0]
    path = p.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return urlunparse(("https", host, path, "", "", ""))


def host_of(url: str) -> str:
    return (urlparse(url).netloc or "").lower().split(":")[0]


def is_blocked_host(url: str) -> bool:
    h = host_of(url)
    return any(b in h for b in BLOCKED_HOST_SUBSTRINGS)


def is_allowed(url: str) -> bool:
    """CANONICALISING gate. True when the url, after normalisation, points at a
    main-domain HTML page that belongs in the index.

    This deliberately ACCEPTS input carrying a query string or fragment and
    strips it, because discovered links on real pages carry tracking parameters
    and a writer pasting a URL from analytics should not be rejected over
    "?utm_source=". What enters the index is always the normalised form.

    It is NOT the SOP Rule 6 gate. Nothing suggested to a writer passes through
    this function alone: every emitted URL is checked by rules.url_ok(), which
    REJECTS query strings and fragments outright. Keep the two separate; folding
    them together would either break link discovery or leak parameters into
    output.
    """
    n = normalise(url)
    if not n:
        return False
    p = urlparse(n)
    if p.scheme != "https":
        return False
    # Subdomains are excluded: the host must be EXACTLY binaytara.org.
    if p.netloc != settings.ALLOWED_HOST:
        return False
    if is_blocked_host(n):
        return False
    path = p.path or "/"
    for rx in _exclude_re:
        if rx.search(path):
            return False
    return True


def section_of(url: str) -> str:
    path = urlparse(normalise(url) or url).path or "/"
    for prefix, name in SECTION_RULES:
        if path.startswith(prefix):
            return name
    return "Static"


def is_contributor(url: str) -> bool:
    return (urlparse(normalise(url) or url).path or "").startswith(CONTRIBUTOR_PREFIX)
