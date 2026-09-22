"""All tunable values live here. Nothing else in the codebase hard-codes a threshold."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

# ---------- Site ----------
SITE = "https://binaytara.org"
SITEMAP_URL = f"{SITE}/sitemap.xml"
ALLOWED_HOST = "binaytara.org"

# ---------- Embedding contract ----------
MODEL_NAME = "BAAI/bge-small-en-v1.5"
EMBED_DIM = 384
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "
QUERY_PREFIX_APPLIED_BY = "application"
EMBED_BATCH = 32

# ---------- Crawl ----------
MAX_CONCURRENCY = 25
REQUEST_TIMEOUT = 20.0
MAX_RETRIES = 3
MAX_REDIRECTS = 5
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
USER_AGENT = "BinaytaraInternalLinker/3.0 (SEO tooling; contact rejish.s@binaytara.org)"

# ---------- Chunking ----------
MIN_BLOCK_WORDS = 20
MAX_CHUNK_WORDS = 120
MIN_BODY_CHARS = 500
MAX_LINK_TEXT_RATIO = 0.5

# ---------- Retrieval ----------
DENSE_K = 30
LEXICAL_K = 30
RRF_K = 60
CANDIDATES_PER_CHUNK = 10
RECEIVE_SOURCE_PAGES = 15

# ---------- Scoring ----------
# v3 RECALIBRATION. v2 measured unrelated p95 at cosine 0.670 and set the hard
# gate at 0.70, which was above p99 (0.719). That killed recall on the full
# 1,680-page index: the same article went from 26 suggestions (v1, too many) to
# 2 (v2, too few). The v3 audit target is 8 to 15 suggestions per article with
# 60%+ precision.
#
# The floor is lowered to p75 of the unrelated distribution. Genuine matches
# still separate clearly above 0.72, but near-misses that keyword evidence or
# title similarity can rescue are no longer silently dropped.
COSINE_NOISE_FLOOR = 0.60
COSINE_SIGNAL_CEIL = 0.82

# Hard gate: below this, a candidate is noise regardless of keyword evidence.
COSINE_HARD_MIN = 0.55

# "Needs insertion" still needs stronger evidence than "Exact in text".
COSINE_INSERTION_MIN = 0.68

W_SEMANTIC = 0.45
W_LEXICAL = 0.20
W_ANCHOR = 0.15
W_KEYWORD = 0.20        # NEW: weight for the keyword-scan signal

ANCHOR_TIER_SCORE = {1: 1.00, 2: 0.90, 3: 0.75, 4: 0.60, 5: 0.35}

MATCH_MULTIPLIER = {
    "Exact in text": 1.00,
    "Synonym in text": 0.95,
    "Partial in text": 0.80,
    "Needs insertion": 0.60,
}

KEYWORD_EVIDENCE_FLOOR = 0.62
KEYWORD_EVIDENCE_MIN_WORDS = 2

SYNTHETIC_PENALTY = 0.80

BAND_HIGH = 0.55
BAND_MEDIUM = 0.38
BAND_MIN = 0.22
CONFERENCE_MIN_SCORE = 0.60

# ---------- SOP rules ----------
MAX_PER_PARAGRAPH = 2
ANCHOR_MIN_WORDS = 2
ANCHOR_MAX_WORDS = 5
CROWDED_PAGE_LINKS = 12

LINK_BENCHMARK = [(500, "2 to 4"), (1000, "4 to 8"), (10 ** 9, "8 to 12")]

# ---------- Topic overlap ----------
OVERLAP_HIGH = 0.60
OVERLAP_MEDIUM = 0.35

# ---------- Keyword cannibalization, real query data (v4) ----------
# Thresholds are in SHARED RANKING QUERIES, from a Semrush organic positions
# export, not title similarity. Generate the map with:
#   python -m engine.cannibalization_data <semrush-export.xlsx>
# Measured on the September 2026 export: 496 keywords rank with more than one
# URL; the worst pair (two ivermectin articles) shares 228 queries.
CANNIBAL_HIGH_QUERIES = 10
CANNIBAL_MEDIUM_QUERIES = 3

# ---------- Awareness and conference matching (v4) ----------
# "[Disease] Awareness Month" pages are overview hubs for a disease. When an
# article is about that disease, the awareness page is almost always a valid
# link target, but embeddings rank it poorly because its text is generic.
AWARENESS_PATTERNS = [
    r"\bawareness\s+month\b", r"\bawareness\s+week\b", r"\bawareness\s+day\b",
]
AWARENESS_MATCH_FLOOR = 0.60      # score floor when disease term matches

# Conference recap pages mentioning a disease are valid targets for articles
# about that disease.
CONFERENCE_DISEASE_FLOOR = 0.52

# ---------- Keyword scan (v3) ----------
# Minimum term length and occurrence thresholds for the keyword scanner.
KEYWORD_MIN_TERM_LEN = 4          # ignore very short terms
KEYWORD_MAX_TERMS = 30            # cap extracted terms per article
# A page-level keyword hit on a core disease term is strong independent evidence.
# It sets a floor under the combined score, rescuing candidates the embedding
# missed (the "alcohol" <> "stomach cancer" failure from the v2 audit).
KEYWORD_HIT_FLOOR = 0.50
# Title similarity threshold for same-topic detection. Two articles whose H1s
# share more than this fraction of content words are considered same-topic and
# always surface as candidates.
TITLE_SIMILARITY_MIN = 0.40
TITLE_SIMILARITY_FLOOR = 0.58     # score floor for same-topic matches

# ---------- LLM rewriting (v3) ----------
LLM_ENABLED = True
LLM_PROVIDER = "groq"             # "groq" | "huggingface" | "none"
LLM_MODEL_GROQ = "llama-3.1-8b-instant"
LLM_MODEL_HF = "mistralai/Mistral-7B-Instruct-v0.3"
LLM_TIMEOUT = 15.0
LLM_MAX_RETRIES = 2

# ---------- De-orphaning ----------
DEORPHAN_BONUS = 0.10
DEORPHAN_CAP = 10

# ---------- App ----------
BATCH_WORKERS = 1
BATCH_MAX = 20
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
INDEX_STALE_DAYS = 10

ANCHOR_GUIDE_CSV = (
    "https://docs.google.com/spreadsheets/d/"
    "1SiecxT_ZffUJiVHXRgswaKeB5HtLxeZGsTo3QXd8z6Q/export?format=csv&gid=84068859"
)

# ---------- Gemini API (v6) ----------
GEMINI_MODEL = "gemini-1.5-flash"
GEMINI_TIMEOUT = 60.0
GEMINI_GIVE_CANDIDATES = 35     # max candidates to send for give-side judgment
GEMINI_RECEIVE_CANDIDATES = 30  # max candidates to send for receive-side judgment

# ---------- Hub page skip (v6) ----------
# When a cancer type has a planned hub page that is NOT yet live,
# skip the existing 101/overview page for that cancer type.
# Set to True ONLY after the first hub pages go live.
HUB_SKIP_101_WHEN_PLANNED = False
