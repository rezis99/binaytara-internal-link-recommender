# Binaytara Internal Linker v7

Gemini reads the article and the candidate pages, decides which links genuinely
help a reader, then writes each anchor and sentence itself. Code handles the
fast filtering, the SOP rules, and the Excel. Nothing is sliced out of a title
by a regex anymore, which is what produced broken anchors in earlier versions.

## Pipeline

```
1. Pre-filter 1,680 pages to ~30 candidates   (code: keyword + disease match)
2. Gemini judges relevance: YES / NO           (1 API call)
3. For each YES, Gemini picks the paragraph,
   the anchor, and writes the full sentence,
   validated and fixed over up to 3 attempts   (1-3 API calls each)
4. SOP rules: one anchor per target, caps,
   contributor naming, conference expiry        (code)
5. One merged Give+Receive sheet per article    (code)
```

Gemini is told to PRODUCE a usable link, not to look for reasons to refuse. A
suggestion is only dropped when there is a genuine topic mismatch, never merely
because the first anchor was imperfect.

## What changed from v6

| Problem in v6 | Fix in v7 |
|---|---|
| Hardcoded `gemini-1.5-flash` (retired) → every call silently failed | Model auto-discovery via the live ListModels endpoint; prefers Flash-Lite for its 500/day free quota; if a listed model 404s it tries the next one |
| One API key, no fallback | Any number of keys (`GEMINI_API_KEY`, `GEMINI_API_KEY_1`, `GEMINI_API_KEY_2`...) pooled with rotation on rate-limit |
| Code sliced anchors from H1 → "Dr Gentry King on Advances", "First Positive" | Gemini owns anchor + sentence generation, validated and retried |
| "Needs insertion" rows shipped as manual instructions | Gemini writes the complete sentence; if it can't after 3 tries the row is dropped, never shown as a chore |
| Receive sentences were lowercased and pulled from a stale index | Receive candidates fetched live, correct case; index lowercasing fixed at the root |
| Listing / homepage / bio pages matched articles to themselves | Excluded from both candidate pools |
| Give and Receive on separate tabs | One merged sheet per article with a Direction column |

## Setup

1. Deploy this repo on Streamlit.
2. In Streamlit → Manage app → Settings → Secrets, add at least one key:
   ```
   GEMINI_API_KEY = "AIza...primary"
   GEMINI_API_KEY_1 = "AIza...second account (optional)"
   GEMINI_API_KEY_2 = "AIza...third account (optional)"
   ```
   Get free keys at https://aistudio.google.com/apikey (one per Google account;
   each account's free quota is separate).
3. Ensure the `data/` folder has the index (`pages.json`, `body_texts.json`,
   `faiss.index`, `paragraphs.json`, `manifest.json`, plus `cannibalization.json`).
4. Reboot the app. The sidebar shows the model actually in use and how many keys
   are pooled, e.g. "✅ Gemini: gemini-3.5-flash-lite (2 keys)".

## Free-tier capacity

Flash-Lite gives ~500 requests/day per key. A typical article uses one
give-judgment call, one receive-judgment call, and 1-3 generation calls per
approved link, roughly 8-15 calls total. One key covers ~20 articles/day; add
keys to scale linearly.

## Reading the output

Each article gets one sheet. The **Direction** column says GIVE (a link to add
inside this article) or RECEIVE (a link another page should add pointing here).
The **Modified Sentence** column is ready to paste. The **Use?** column is blank
for the reviewer. A "Why these results?" panel in the app shows exactly how many
candidates were found, judged relevant, and produced at each stage, so an empty
result is never a mystery.
