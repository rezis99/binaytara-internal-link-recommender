---
name: internal-link-filter-and-rewrite
description: >
  Post-process a Binaytara Internal Link Recommender Excel file. Does two jobs
  in one pass: (1) filters out irrelevant suggestions that the embedding model
  included because of shared oncology vocabulary, and (2) rewrites every "Needs
  insertion" sentence with the anchor phrase woven in naturally. Use when the
  user uploads an .xlsx from the internal link recommender tool. Triggers on
  "filter and rewrite", "clean up this file", "make these writer-ready",
  "review the suggestions", or "finalize the link suggestions."
---

# Internal Link Filter and Rewriter

## Purpose

The Binaytara Internal Link Recommender tool scans 1,680 pages in seconds using
embeddings. It retrieves candidates fast but judges them poorly: on an oncology
site where every page shares "cancer," "treatment," and "immunotherapy," the
math cannot tell cancer types apart. This skill adds human-quality judgment.

You (Claude) read each suggestion, decide whether it genuinely helps a reader,
remove what does not, and rewrite the "Needs insertion" sentences.


## When to trigger

The user uploads a .xlsx AND says "filter", "rewrite", "clean up", "finalize",
"review", "writer-ready", or "link suggestions."


## Reading the Excel

Column layout (v5):
  Use? | Relevance | Match Type | Keyword Competition | Anchor Text |
  Target Page Title (or Source Page Title) | Target Page Link (or Source Page) |
  Section | Existing Sentence | Modified Sentence | Review Context | Notes

Sheets: "Give - ...", "Other - ...", "Recv - ...", "Summary"

On Recv sheets, the target URL is in a comment on cell A1 (hover to see it).


## PHASE 1: FILTERING

Read every data row. For each, decide: YES (keep) or NO (reject).

### Filter rules (apply all, in order)

F1: DIFFERENT DISEASE = NO.
If the source article is about kidney cancer and the target is about breast
cancer, reject. Shared vocabulary ("immunotherapy", "checkpoint inhibitors")
is not shared topic.
Exception: articles that explicitly compare multiple cancer types.

F2: DUPLICATE ANCHOR = PICK ONE.
If the same anchor text (e.g., "kidney cancer") appears in multiple rows
pointing to DIFFERENT URLs, approve only the single best target. Mark the
others: "ALTERNATIVE: use this OR row X, not both."
Best = the target most specific to the article's topic. If the article
discusses kidney cancer immunotherapy, prefer "Kidney Cancer Adjuvant IO"
over "Kidney Cancer Awareness Month."

F3: ONE LINK PER DISEASE PER ARTICLE.
After approving one link for a disease (e.g., "breast cancer" → Breast Cancer
101), flag subsequent links to the same disease as: "REDUNDANT: breast cancer
already covered in row X." The writer can override this, but should know.

F4: GENERIC OR MEANINGLESS ANCHOR = NO.
Reject anchors that are:
  Generic phrases: "all cancer", "risk factors", "early detection",
  "clinical trials", "treatment options", "new study", "new research"
  Title fragments without topical meaning: "Three Key Pillars For Improving"
  Geographic names used as anchors: "North India", "Tanzania"
  Broken encoding: anchors with "Nepal s" or similar (space before "s"
  indicates a lost apostrophe; flag it as an encoding issue)

F5: ORGANIZATIONAL ≠ TOPICAL = NO.
Reject links to volunteer pages, donation pages, partnership pages, community
health assessments, or any page related to Binaytara as an organization but
not to the article's medical topic.

F6: CONFERENCE MUST MATCH THE DISEASE = NO.
A conference about breast cancer is not relevant to a kidney cancer article
just because both mention "ASCO." Only keep conference links where the
conference specifically covers the source article's disease.
Also reject event/conference blog posts ("Binaytara Opens Breast Cancer
Awareness Month with Women's Cancer Conference in Portland") unless the
article specifically discusses that event.

F7: JOURNAL (IJCCD) MUST MATCH THE STUDY = NO.
A SEER analysis of stomach cancer is relevant to a stomach cancer article.
A study of health needs in Janakpurdham is not relevant to an alcohol article.

F8: MEDICAL CLAIM IN REWRITE = NO.
If the Modified Sentence adds a medical recommendation, statistic, or drug
name not in the Existing Sentence, reject the entire row. Example: a rewrite
that adds "making regular Mammogram Screening important" introduces a medical
recommendation. The writer should not need to fact-check the tool's output.

F9: RECIPROCAL LINK = FLAG.
If the Notes say "already receives a link FROM this article" or similar, the
suggestion would create a two-way link. This CAN be valid, but note it:
"Would create a reciprocal pair. Both pages would link to each other."

F10: EXISTING LINK IN PARAGRAPH = FLAG.
If the Notes mention "This paragraph already has N link(s)", do not
automatically reject, but note: "This paragraph already contains a link.
The writer should check whether adding a second link crowds the paragraph."

F11: HUB PAGE PLANNED = NOTE.
If the Review Context mentions a planned /cancer-types/ hub page, add a note:
"A dedicated hub page for this cancer type is planned. Use the current best
page for now; update when the hub goes live." Do NOT reject the suggestion.

F12: KEEP AWARENESS AND OVERVIEW PAGES.
"[Disease] Awareness Month" and "[Disease] 101" pages for the article's own
disease should almost always be YES (unless another row already covers that
disease).

F13: KEEP RISK-FACTOR CROSS-LINKS.
Alcohol article → esophageal cancer, stomach cancer, breast cancer are all
valid because alcohol is a confirmed risk factor for each. Do NOT reject
these as "different disease."

F14: WHEN IN DOUBT = NO.
Better to miss a marginal link than to waste the writer's time.


## PHASE 2: REWRITING

For every row that passed filtering AND has Match Type "Needs insertion":

### Rewrite rules

R1: ANCHOR VERBATIM. The phrase must appear exactly as given.

R2: NO NEW MEDICAL CLAIMS. No statistics, drug names, survival rates, or
recommendations not in the original. "Making regular mammogram screening
important" is a medical recommendation. Do not add it.

R3: PRESERVE MEANING. Do not drop clauses, change numbers, or alter conclusions.

R4: NATURAL PHRASING. No "for more information about" or "learn more about."
Weave the anchor into an existing clause or add a brief subordinate clause.

R5: LENGTH 0.7x to 1.5x of original word count.

R6: LINK FORMAT. [anchor](url) exactly once.

R7: QUOTE PROTECTION. Sentence in quotation marks = direct quote. Do NOT
rewrite. Mark: "[Direct quote: place link on adjacent sentence instead.]"

R8: JOURNAL = CITATION STYLE. Frame as "as reported in a [study](url)"
rather than body prose.

R9: SENTENCE WITH EXISTING LINK. If the Notes say the paragraph already has
a link, check if the existing link is EXTERNAL and Binaytara has a page on
the same topic. If yes, the rewrite can suggest replacing the external link
with the Binaytara internal link. If the existing link is internal, suggest
placing the new link on a different sentence in the same paragraph.

### Validation (check every rewrite)

  Anchor appears verbatim (case-insensitive word boundary)
  [anchor](url) is correctly formed
  Word count is 0.7x to 1.5x of original
  No stray markdown outside the one link
  No meta-commentary ("here is", "note:", "rewritten:")
  No medical claims added (scan for numbers, drug names, "important",
  "recommended", "should", "regular" that were not in the original)


## PHASE 3: OUTPUT

### Write back to Excel

  YES rows: green "YES" in Use? column
  NO rows: "NO" in red in Use? column; gray strikethrough font on the row;
  rejection reason in Notes
  ALTERNATIVE rows: "ALT" in orange; reason says which row to pick instead
  REDUNDANT rows: "DUP" in orange; reason says which row already covers this
  Rewritten sentences: blue font (#1F3864) in Modified Sentence column
  Failed rewrites: red font; "[auto-rewrite failed]" appended
  Direct quotes: red font; "[Direct quote]" note

### Summary table (always show this)

```
FILTER + REWRITE SUMMARY

  Total suggestions in file:           83
  Filtered out (irrelevant):           48
  Kept (genuinely relevant):           35
    Already writer-ready:              22
    "Needs insertion" rewritten:       11
    Direct quotes (skipped):            1
    Rewrite failed:                     1

DUPLICATE ANCHOR RESOLUTIONS:
  "kidney cancer" → Approved for Kidney Cancer Adjuvant IO (row 5)
                  → Marked ALT: Kidney Cancer Awareness (row 3)
                  → Marked ALT: HIF-2α Inhibition (row 7)
  "stomach cancer" → Approved for Stomach Cancer 101 (row 2)
                   → Marked ALT: IJCCD racial disparities (row 8)

DISEASE COVERAGE PER ARTICLE:
  Alcohol Cancer Risk: breast cancer (row 1), stomach cancer (row 2),
    esophageal cancer (row 4). Breast awareness (row 3) marked REDUNDANT.

FILTER DECISIONS:
| Sheet       | Row | Anchor            | Target                    | Decision | Reason |
|-------------|-----|-------------------|---------------------------|----------|--------|
| Give - kidn | 3   | kidney cancer     | Awareness Month           | ALT      | Same anchor as row 5; row 5 is more specific |
| Give - kidn | 5   | kidney cancer     | Adjuvant IO               | YES      | Best target for this anchor |
| Give - alco | 4   | mammogram screen  | Mammogram Myths           | NO       | Rewrite adds medical recommendation |

REWRITE DECISIONS:
| Sheet       | Row | Anchor          | Status    | Preview (80 chars) |
|-------------|-----|-----------------|-----------|--------------------|
| Give - kidn | 5   | kidney cancer   | REWRITTEN | ...advances in [kidney cancer](https://bi... |
| Recv - alco | 3   | alcohol use     | QUOTE     | [Direct quote: place link on adjacent...] |
```

End with: "Review the YES and REWRITTEN rows. Gray rows are filtered out.
Orange ALT/DUP rows are alternatives the writer can pick instead of the
approved row if they prefer."


## Examples

### Duplicate anchor resolution
Rows for "kidney cancer":
  Row 3: Kidney Cancer Awareness Month (awareness hub)
  Row 5: Adjuvant IO in Kidney Cancer (same disease, treatment angle)
  Row 7: HIF-2α Inhibition in Kidney Cancer (same disease, drug angle)
Decision: Row 5 is most specific to an immunotherapy article. YES for row 5.
ALT for rows 3 and 7 with note: "Pick this OR row 5."

### Risk-factor cross-link (KEEP)
Source: Alcohol and Cancer Risk
Target: Esophageal Cancer Awareness
Anchor: "esophageal cancer"
Decision: YES. Alcohol is a confirmed risk factor for esophageal cancer.
The article discusses esophageal cancer specifically.

### Conference blog post (REJECT)
Source: Alcohol and Cancer Risk
Target: "Binaytara Opens Breast Cancer Awareness Month with Conference"
Decision: NO. This is an event announcement, not a topical page about
alcohol or cancer risk.

### Medical claim in rewrite (REJECT)
Existing: "Alcohol is also linked to cancers of the breast."
Modified: "Alcohol is linked to cancers of the breast, making regular
[Mammogram Screening](url) important for early detection."
Decision: NO. "Making regular mammogram screening important" is a medical
recommendation not in the original sentence.

### Sentence with existing external link
Existing: "November is Stomach Cancer Awareness Month, dedicated to raising
awareness..." (links to nostomachforcancer.org)
Notes: "This paragraph already has 1 link(s): https://nostomachforcancer.org/..."
Decision: Check if another sentence in the article can carry the internal
"stomach cancer" link instead. If yes, suggest that sentence. If no, note:
"Consider replacing the external link with the internal Binaytara page, but
verify the external link is not editorially required."


## What NOT to do

Do NOT approve based on the tool's relevance score alone. The score uses
embedding similarity, which is the exact signal that fails on oncology content.

Do NOT change the anchor phrase. "Stomach cancer" stays "stomach cancer."

Do NOT add medical claims in rewrites. If uncertain whether something is a
medical claim, it is. Leave it out.

Do NOT touch the Summary sheet.

Do NOT approve more than 2 links per paragraph. If a paragraph already has
2 approved rows, mark additional rows as "MAX PER PARAGRAPH: 2 links
already approved for this paragraph."
