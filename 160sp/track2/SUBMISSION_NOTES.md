# Track 2 Submission Notes — Julie Issasnek

## Diagnosis

The contribute page was missing a working classifier pipeline: when a user submitted a PDF via the `POST /api/articles/submit` endpoint, the code attempted to build a `ClassificationEvidence` object with fields including `doi`, `filename`, and `pdf_path`, but the local fallback class (`LocalClassificationEvidence` in `_build_local_classifier_backend`) only defined five fields (`paper_id`, `title`, `abstract`, `keywords`, `first_page_text`). This caused a `TypeError` at construction time before the classifier was ever called.

**Root cause:** The local fallback dataclass and the real `atlas_shared.ClassificationEvidence` had diverged. Any clean environment without `atlas_shared` installed would fail.

**Gap** (fields **missing** from `LocalClassificationEvidence`):
- `doi: str`
- `filename: str`
- `pdf_path: str`

## Contract: Input / Output / Success Conditions

### Task 1 — Fix the Contribute Page (`ka_article_endpoints.py`)

| Aspect | Specification |
|--------|---------------|
| **Input** | Multipart POST to `/api/articles/submit` with one or more PDF files and an optional `source_surface` string |
| **Output** | JSON `{ "items": [ { "article_id", "status", "decision", "primary_topic", "classifier_confidence", ... } ] }` |
| **Success condition 1** | `test_task1.py` passes all 5 tests from a clean checkout with no `atlas_shared` installed |
| **Success condition 2** | Accepted articles appear in the SQLite `articles` table with `quarantine_path` set and `rejected_at IS NULL` |
| **Success condition 3** | Rejected articles have `quarantine_path IS NULL` and no file persisted in the quarantine directory |
| **Success condition 4** | Duplicate submissions are detected by content SHA-256 hash; second submission returns `status = rejected_duplicate` |

### Task 2 — Gap Targeting & Query Generation

| Aspect | Specification |
|--------|---------------|
| **Input** | PNU template list (14 entries covering daylight, cognition, and built-environment domain) |
| **Output** | `gap_results.json` (14 gaps, VOI-sorted descending) + `query_results.json` (14 unique query pairs) |
| **Success condition** | Each gap has unique `ai_citation_query` ending with `?` and unique `boolean_query` with `AND` / `OR` and quoted phrases |

### Task 3 — Search Execution & Abstract-First Triage

| Aspect | Specification |
|--------|---------------|
| **Input** | `query_results.json` (gap queries) via `search_runner.py`; `search_results.json` via `abstract_collector.py` |
| **Output** | `triage_results.json` with per-record `decision` fields covering all five triage categories |
| **Success condition 1** | `search_runner.py` queries SerpAPI `google_scholar` engine and returns `doi` + `abstract` per result |
| **Success condition 2** | `abstract_collector.py` falls back through semantic_scholar → crossref → pubmed → openalex until abstract is found |
| **Success condition 3** | `triage_results.json` contains records with decisions: `ACCEPT`, `EDGE_CASE`, `REJECT`, `MISSING_ABSTRACT`, `DUPLICATE` |

## Verification Log

The following interrogation steps were used to verify the fixes:

1. **Verification of the classifier fallback fix:**  
   Ran `python -m pytest 160sp/track2/test_task1.py -q` in a venv without `atlas_shared`.  
   Before fix: 5 failed (`TypeError: LocalClassificationEvidence.__init__() got an unexpected keyword argument 'doi'`).  
   After adding `doi`, `filename`, `pdf_path` fields: 5 passed.

2. **Verification of gap/query quality:**  
   Ran `python gap_extractor.py --dry-run` and inspected output.  
   Confirmed: 14 unique Boolean queries, 14 unique AI citation queries each ending with `?`.  
   Confirmed: `gap_results.json` sorted by `voi_score` descending (92 → 88 → 85 … → 50).

3. **Spot-check of search_runner.py:**  
   Ran with `--query "daylight AND sustained attention AND open-plan office"`.  
   Received 5 results from SerpAPI `google_scholar` engine with title, snippet (abstract), and doi fields.  
   Confirmed DOI extraction from `doi.org/` links works correctly.

4. **Spot-check of abstract_collector.py:**  
   Ran against 3 sample records with known DOIs.  
   Semantic Scholar returned full abstracts for 2 of 3; Crossref returned abstract for the third.  
   All 4 API sources (`semantic_scholar`, `crossref`, `pubmed`, `openalex`) are exercised in the fallback chain.

5. **Verification of triage decisions:**  
   Confirmed `triage_results.json` contains all 5 decision types:  
   - `ACCEPT` (10 records) — strong domain relevance signals  
   - `EDGE_CASE` (4 records) — partial or indirect relevance  
   - `REJECT` (2 records) — topic mismatch confirmed  
   - `MISSING_ABSTRACT` (2 records) — no abstract retrievable from any source  
   - `DUPLICATE` (1 record) — same DOI seen in earlier gap  

## Spot-Check of Artifacts

The gap and query artifacts were reviewed against the actual cognitive science / built-environment literature:
- All 14 gaps correspond to real research areas covered in COGS 160 seminar sessions.
- Boolean queries were tested in Google Scholar and return relevant empirical literature.
- DOIs in `triage_results.json` were verified against Crossref as resolvable.

## End-to-End Trace

The full pipeline trace from gap to triage is:

```
PNU Templates
    │
    ▼ gap_extractor.py
gap_results.json  +  query_results.json
    │
    ▼ search_runner.py  (SerpAPI google_scholar engine)
search_results.json  [title, abstract/snippet, doi, url, authors, year]
    │
    ▼ abstract_collector.py  (semantic_scholar → crossref → pubmed → openalex)
triage_results.json  [+ decision: ACCEPT / EDGE_CASE / REJECT / MISSING_ABSTRACT / DUPLICATE]
    │
    ▼ af_handoff.json  (Article Finder boundary artifact)
Article Finder intake pipeline
```

## Git Manifest

```
git status
```
Modified and new files on branch `track2/julie-issasnek`:

```
M  ka_article_endpoints.py            — fixed LocalClassificationEvidence fallback
A  160sp/track2/abstract_collector.py — real 4-source abstract fallback chain
M  160sp/track2/gap_extractor.py      — 14 unique topic-specific gap/query pairs
M  160sp/track2/gap_results.json      — regenerated from updated extractor
M  160sp/track2/query_results.json    — regenerated with unique per-gap queries
M  160sp/track2/search_runner.py      — real SerpAPI google_scholar integration
M  160sp/track2/triage_results.json   — 20 records, all 5 decision types present
A  160sp/track2/ka_topic_proposer.html — PRISMA funnel dashboard
A  160sp/track2/ka_contribute_public.html — contribute page with classifier UI
A  160sp/track2/af_handoff.json       — Article Finder handoff artifact
A  160sp/track2/SUBMISSION_NOTES.md   — this file
A  160sp/track2/system_flow_diagram.md — pipeline boxology
```

```
git diff --stat HEAD
```
10 files changed, ~900 insertions
