# Binaytara Internal Link Recommender: Setup and Operation Guide

Follow this top to bottom. Every command is meant to be copied as written.

Total time: roughly 90 minutes, most of it waiting for the first index build.
Total cost: zero.

---

## 0. What you are installing

A Streamlit web app that takes one binaytara.org article (a live URL or an
unpublished `.docx` draft) and returns two tables:

- **Links to Give:** where in this article to place links out to existing pages
- **Links to Receive:** which existing pages should add a link pointing to it

Both download as one formatted Excel workbook you can hand to Bibek, Ariana or Kaila.

The site is crawled and indexed **offline** by GitHub Actions twice a week. The
app only loads the prebuilt index, which is what makes it fit inside a free host.

### What is verified, and by whom

Two different things, kept separate on purpose. A maintainer reading this later
should be able to tell which claims they can re-check themselves.

**A. Reproducible from this package, on your machine, no network needed:**

| Check | Command |
|---|---|
| 61 rule tests | `python tests/test_rules.py` |
| 41 end-to-end tests: artifact load, HTML pipeline, DOCX pipeline, workbook | `python tests/test_end_to_end.py` |
| Clean install and app boot | `pip install -r requirements.txt && streamlit run app.py` |

**B. Observed against the live site during the build session (September 16, 2026),
not reproducible without network access.** Re-measure these yourself in step 7:

| Observation | Value seen |
|---|---|
| Sitemap discovery | 1,680 indexable pages across 7 sections |
| Content extraction | Worked on TCN, IJCCD, Blog, Conference, Contributor, Static |
| Peak memory, 368-page index | 395 MB, against a 690 MB documented floor |
| Anchor guide sheet | Readable, 35 target URLs loaded |

Treat column B as a starting expectation, not a guarantee. If your numbers differ
materially, that is information about the site, not a broken tool.

---

## 1. Prerequisites

| Thing | Why | Cost |
|---|---|---|
| A GitHub account | Hosts the code and runs the twice-weekly index build | Free |
| A Streamlit Community Cloud account | Hosts the app; sign in with GitHub | Free |
| Python 3.12 locally | Only if you want to run it on your laptop first | Free |

---

## 2. Do this first: the nonprofit resource request

Streamlit grants increased resources to nonprofits on a case-by-case basis. It
costs nothing but a form and it changes your whole resource envelope.

1. Open https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app
2. Find the "Good for the world" section and open the linked application form
3. Submit it with: Binaytara, 501(c)(3), EIN 26-1603676, an internal editorial
   tool for a cancer-education publisher

Do this **now**, before step 7, so the deployment test measures the envelope the
tool will actually live in.

---

## 3. Put the code on GitHub

```bash
cd ~/Downloads
unzip binaytara-internal-linker.zip
cd binaytara-internal-linker

git init
git add .
git commit -m "Internal link recommender: initial build"
```

Create an **empty** repository on GitHub named `binaytara-internal-linker`, then:

```bash
git remote add origin https://github.com/rezis99/binaytara-internal-linker.git
git branch -M main
git push -u origin main
```

Use a **public** repository if you can: GitHub Actions minutes are unlimited on
public repos, and the tool contains no secrets. If it must be private, you get
2,000 free minutes a month, which is far more than eight runs need.

---

## 4. Run it locally once (recommended)

This proves the whole pipeline before any deployment.

```bash
python3.12 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Run both suites. Everything must pass before you go further:

```bash
python tests/test_rules.py          # 61 rule-level checks
python tests/test_end_to_end.py     # 41 integration checks against the shipped index
```

The second suite is the one that matters operationally: it loads the real FAISS
and BM25 artifacts, runs a full analysis on an HTML fixture and a DOCX fixture,
and reopens the generated workbook. Passing rule tests alone does not prove the
application works.

The package ships with a **partial index of 368 pages**, a stratified sample
across every section, so you can try the app immediately:

```bash
streamlit run app.py
```

Paste any binaytara.org article URL and click Analyse.

---

## 5. Build the full index

The shipped index is a sample. Build the real one over all 1,680 pages:

```bash
python -m indexer.build_index
```

Expect 25 to 45 minutes, almost all of it embedding on CPU. You will see:

```
1/6 discovering URLs from sitemap
2/6 crawling 1680 pages
3/6 extracting content
4/6 counting inbound links
5/6 embedding
6/6 building lexical index and writing artifacts
```

The build **aborts without writing anything** if any of these trip:

| Gate | Threshold |
|---|---|
| Crawl failures | more than 10 percent of sitemap URLs |
| Pages fetched but unusable after extraction | more than 25 percent |
| Page count against the previous index | dropped below 80 percent |
| Probable firewall block | more than 5 empty 403 responses |

The second one is the important one: a site template change can let every page
return HTTP 200 and still yield nothing usable. Crawl success alone does not
prove the index is sound. Canonical collisions, where two URLs resolve to the
same canonical page, are counted and listed in the manifest rather than silently
overwriting each other.

A stale index is recoverable. A silently truncated one looks healthy and produces
wrong suggestions for weeks.

Commit the result:

```bash
git add data/
git commit -m "Full site index"
git push
```

**Useful diagnostic:** step 4 prints how many pages have zero inbound body links.
That is your orphan count, measured directly rather than estimated.

---

## 6. Deploy to Streamlit Community Cloud

1. Go to https://share.streamlit.io and sign in with GitHub
2. Click **New app**
3. Repository `rezis99/binaytara-internal-linker`, branch `main`, main file `app.py`
4. Click **Deploy**

First deploy takes 5 to 10 minutes while dependencies install. You get a URL like
`https://binaytara-internal-linker.streamlit.app` that you can send to the team.

---

## 7. The memory test (do not skip this)

Everything above assumed the app fits. Now prove it. Run each of these on the
**deployed** app, not locally:

| Test input | What you are watching for |
|---|---|
| Five recent TCN articles | Any resource error |
| One article over 3,000 words | Peak memory |
| One IJCCD article | The placeholder meta description path |
| One article with 10+ existing links | Crowding notes appear |
| One article dense with physician quotes | Quotes are skipped |
| One `.docx` draft upload | Upload path works |
| Two browser tabs at once | Concurrency cost |

**Pass conditions, all four required:**

1. No "This app has gone over its resource limits" and no "Oh no" page
2. Single-article analysis completes in under 30 seconds once warm
3. Two concurrent sessions do not trigger an error
4. Cold start from the sleeping page completes

**If it fails,** do not raise the memory budget and hope. Reduce, in this order:
store the FAISS index as float16; memory-map `paragraphs.json`; move the BM25
corpus to disk. Only then reconsider the host.

Until this passes, keep batch mode unused.

---

## 8. Turn on the twice-weekly index update

The workflow is already in the repository at
`.github/workflows/update_index.yml`. Enable it:

1. Open your repo on GitHub, click **Actions**, enable workflows if prompted
2. Go to **Settings > Actions > General > Workflow permissions** and select
   **Read and write permissions**, then Save. Without this the bot cannot commit
   the updated index
3. Click **Actions > Update page index > Run workflow** to test it manually
   before trusting the schedule

It then runs Monday and Wednesday at 06:00 Nepal time.

**The workflow will not commit a bad index.** It runs the rule tests before
rebuilding, then runs the end-to-end suite against the freshly written artifacts,
and only commits if both pass. It also uploads the manifest as a workflow
artifact and raises a warning if a build exceeds 30 minutes, well before the
45-minute job timeout.

**On the name "update".** `indexer/update_index.py` detects what changed and then
rebuilds the entire corpus. It is not an incremental artifact update. At the
current size that is the right trade, because a full rebuild removes a whole
class of merge bugs. Watch `build_seconds` in the manifest; if it climbs toward
30 minutes, that is the signal to build a real incremental path.

**A note on the schedule.** Nepal is UTC+05:45, so 06:00 NPT is 00:15 UTC on the
same day, which is cron day-of-week `1` for Monday, not `0`. The file has the
correct values. GitHub also delays scheduled jobs under load and disables
schedules on repositories with no activity for 60 days, so if the app's sidebar
warns the database is over 10 days old, check the Actions tab.

---

## 9. How the team uses it

Send them the app URL. No login.

**During writing (Phase B of the QA workflow):** paste the draft URL, or upload
the Google Doc exported as `.docx`. Work from the **Links to Give** tab.

**After publishing:** paste the new article's URL and work from **Links to
Receive**. This is what closes SOP Rule 8, adding links from 2 to 3 existing
pages to the new article, and it is the de-orphaning workflow.

**Reading the tables:**

| Column | What to do with it |
|---|---|
| Existing Sentence | The sentence in the article as it stands now |
| Modified Sentence | Copy this in, or follow the instruction if the phrase is absent |
| Anchor Text | The clickable text, 2 to 5 words per SOP Rule 4 |
| Relevance | 🟢 High, 🟡 Medium, ⚪ Lower |
| Match Type | "Exact in text" needs no rewriting; "Needs insertion" does |
| Topic Overlap (heuristic) | Red means both pages target similar keywords. Judge before linking |
| Notes | "Recommended" versus "Alternative", inbound link count, conference dates |

**Tell the writers two things:**

1. Every suggestion is a suggestion. The tool proposes; the writer decides.
2. Do not upload patient-identifiable information or confidential or embargoed
   manuscripts.

**What "never stored" actually means, precisely.** The code never writes an
upload to disk, never logs its text, and never adds it to the index. It is held
in process memory for the duration of your session and in Streamlit session state
until you close the tab or the app restarts. That is a statement about this
codebase, not about the host: what a hosting provider retains in its own logs or
infrastructure is outside the tool's control and is not something this package
can demonstrate.

So the honest position is: for published articles and ordinary drafts, the public
app is fine. If Binaytara decides drafts routinely contain embargoed findings or
anything patient-identifiable, the answer is a private deployment, not a warning
banner. Make that call before writers start uploading real manuscripts, not after.

---

## 10. Before you trust the output: the golden set

This is the part that decides whether the tool is useful or merely plausible.
Nothing in the configuration is validated until you do it.

Pick 20 published articles and hand-write the internal links a careful SEO would
place. Include hard cases deliberately: two articles on closely related cancer
types, two heavy with physician quotes, two already carrying 10+ links, two very
short news items, one IJCCD article, one mentioning a contributor by surname only.

Score three things **separately**, because they point at different code:

| Dimension | A low score means |
|---|---|
| Target relevance | Retrieval: the model, RRF, the synonym map |
| Placement quality | Chunking and the block classifier |
| Anchor quality | The anchor tier logic and the stoplists |

**Target: 80 percent precision in the top 5, and 80 percent acceptable anchors on
High rows.** Below that, tune before releasing.

Have a second person label 5 of the 20 independently. If you disagree on more than
a fifth, the disagreement is about what a good internal link is, and that belongs
in the SOP before any threshold number means anything.

### The five things to tune, in `config/settings.py`

| Setting | Default | Sweep |
|---|---|---|
| `MATCH_MULTIPLIER["Needs insertion"]` | 0.60 | 0.55 to 0.85. The single most consequential value |
| `BAND_HIGH` / `BAND_MEDIUM` | 0.62 / 0.45 | Move together |
| `CONFERENCE_MIN_SCORE` | 0.70 | Raise if bad conference links appear |
| `RRF_K` | 60 | 10 concentrates on top hits; 100 flattens |
| `W_SEMANTIC` / `W_LEXICAL` / `W_ANCHOR` | 0.55 / 0.25 / 0.20 | Must sum to 1.0 |

---

## 11. Routine maintenance

**Extend the synonym map.** `config/oncology_synonyms.json` ships with 72 entries.
It is the cheapest accuracy improvement available and it targets exactly the gap a
smaller model has. Add terms as TCN covers new topics.

**Refresh the anchor guide.** The tool pulls your Internal Linking Anchor Guide on
every index build. Anchors you add there are used first and get a scoring bonus.
The sheet must stay link-shareable; if it fails the build continues without it.

**When the site template changes,** extraction may break. Symptom: page count
drops sharply, or suggestions stop appearing. Fix `config/selectors.py` and run a
full rebuild from Actions with the `full_rebuild` input checked.

---

## 12. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| "No page database found" | `data/` was not committed | Run `python -m indexer.build_index`, commit `data/` |
| "Index was built with ... but settings specify ..." | Model changed without rebuilding | Rebuild the index |
| "This app has gone over its resource limits" | Memory | Reboot from Manage app, then apply the step 7 reductions |
| Sidebar says the database is stale | Actions did not run | Check Actions tab; confirm write permissions |
| Very few suggestions | Section filter too narrow, or lower relevance hidden | Widen the filter; tick "Show lower relevance" |
| Bad suggestions on one topic | Vocabulary gap | Add the terms to `oncology_synonyms.json` |
| Build aborts with ">10% failed" | Site down, or firewall blocking | Retry later; lower `MAX_CONCURRENCY` in settings |

---

## 13. What is deliberately NOT in this build

| Not included | Why |
|---|---|
| LLM sentence rewriting | Phase 2. "Needs insertion" rows give an instruction instead. An LLM that silently alters a survival figure while adding a hyperlink is a clinical accuracy problem, not a copy problem |
| Batch mode at more than 1 worker | `BATCH_WORKERS = 1` until step 7 passes. The value of 4 was picked before any measurement existed |
| Real keyword data for topic overlap | Uses an H1 proxy. A free Google Search Console or Semrush CSV export would replace it; see below |
| Login | Agreed. The index is built from already-public pages. But see the retention note below: that reasoning covers the index, not uploads |
| Orphan flagging | You asked to leave it out. The inbound count still shows in Notes |

**The highest-value free upgrade is data, not hardware.** A GSC query-to-page CSV
export, or a position export from the Semrush Basic plan you already pay for,
would replace the H1 proxy with real query data at zero cost. No API subscription
is needed; both export from the web interface.

---

## 14. Known limits, honestly

- `bge-small` is a smaller model than ideal, chosen so the app fits the free
  host's documented 690 MB floor rather than only its 2.7 GB ceiling. If the
  golden set shows retrieval is the bottleneck, escalate cheapest-first: the
  nonprofit resource increase, then the synonym map, then a cross-encoder
  reranker, and only then a paid host.
- Hub and listing pages are indexed as **link targets only**. No link can be
  placed inside a card grid, so they never appear in Links to Receive.
- Topic overlap is a heuristic and is labelled as one. It detects topical
  similarity, not query-level cannibalization.
- Conference pages are only suggested when the Event schema shows the event has
  not ended. Where the schema is missing the tool falls back to a year in the URL
  or title and drops the page when it cannot tell. Ask Pawan or Pratik to add
  `Event` JSON-LD to conference pages; it is the proper fix.
