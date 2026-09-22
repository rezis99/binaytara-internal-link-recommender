# Binaytara Internal Linker v6

Gemini judges relevance. Code handles anchors and formatting. Each does what
it's best at.

## How it works

```
1. TOOL pre-filters 1,680 pages to ~35 candidates (keyword scan + disease matching)
2. GEMINI judges each candidate: YES or NO with reason (one free API call)
3. TOOL selects anchor from destination page's H1 (string matching, guaranteed correct)
4. TOOL finds that anchor in the source article text (exact search)
5. TOOL builds the Modified Sentence ([anchor](url) insertion)
6. TOOL formats the Excel (Use? column, colors, tabs)
7. CLAUDE SKILL rewrites "Needs insertion" sentences (optional, upload the Excel)
```

## Setup

1. Create a new Streamlit app from this repo
2. Get a free Gemini API key: https://aistudio.google.com/apikey
3. Add it as a Streamlit secret: GEMINI_API_KEY = "AIza..."
4. Trigger an index build from GitHub Actions
5. Reboot the Streamlit app

## Rate limits

20 articles per day uses 2.7% of Gemini's free daily budget.
Gemini 1.5 Flash: 1,500 requests/day, 15 requests/minute.

## What Gemini decides vs what code decides

| Task | Who | Why |
|---|---|---|
| Is this page relevant to this article? | Gemini | Understands kidney ≠ breast despite shared vocabulary |
| What anchor text describes the destination? | Code (anchor.py) | Extracts from H1, guaranteed to match |
| Where in the article does the anchor appear? | Code (string search) | Exact match, cannot hallucinate |
| One anchor one target | Code | Prevents signal collisions |
| SOP rules (R1 to R11) | Code | Deterministic, no judgment needed |
| Statistics-list skip | Code (blocks.py) | Counts cancer types per sentence |
| Hub page skip | Code (hub_pages.json) | Config-driven |
| Excel formatting | Code (excel_writer.py) | Colored sheets, Use? column, tabs |
| Sentence rewriting | Claude skill (optional) | Natural language, medical accuracy |

## Section filters

| Filter | What | Receive side |
|---|---|---|
| TCN | Cancer News articles | Included |
| IJCCD | Journal papers | Excluded (cannot edit published research) |
| Blog | Organizational news | Included |
| Conference | Event pages | Included (with date check) |
| Project | Hubs (OncoBlast, etc.) | Included |
| Static | Evergreen (about, grants) | Included |
| Contributor | Author profiles | Included (only when named in text) |

## Hub page handling

45 cancer type hub pages are planned under /cancer-types/ but none are live.
When HUB_SKIP_101_WHEN_PLANNED is True (default), existing 101 pages for
those cancer types are skipped. When you build a hub, edit hub_pages.json
and set "live": true. The tool then suggests the hub page instead.
