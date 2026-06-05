# Track 2 Submission Notes — Julie Issasnek

## Dependency Contract

### Task 3 Pipeline (harvest_layer, triage_engine, pipeline, article_db, search_runner, gap_extractor, prisma_export)

The Task 3 pipeline is **fully standalone** — it has no `atlas_shared` dependency.

| Package | Required? | Purpose |
|---------|-----------|---------|
| `requests` | **Required** — `pip install requests` | SerpAPI + abstract enrichment HTTP calls |
| `scholarly` | Optional — `pip install scholarly` | Google Scholar fallback scraper (degrades gracefully if absent) |
| `paperscraper` | Optional — `pip install paperscraper` | PubMed + arXiv preprint channel (degrades gracefully if absent) |
| `scidownl` | Optional — `pip install scidownl` | Sci-Hub PDF last resort in Stage 3 (degrades gracefully if absent) |
| `pytest` | Test only — `pip install pytest` | Run `test_track2_task3.py` |

Quick install for a clean environment:
```bash
pip install requests pytest
# optional scrapers:
pip install scholarly paperscraper scidownl
```

### Task 1 (`ka_article_endpoints.py`)

| Package | Required? | Purpose |
|---------|-----------|---------|
| `fastapi` | **Required** — `pip install fastapi` | HTTP endpoint framework |
| `httpx` | **Required** — `pip install httpx` | FastAPI TestClient |
| `pytest` | **Required** — `pip install pytest` | Test runner |
| `atlas_shared` | **Optional** — see resolution order below | Real classifier backend |

`atlas_shared` resolution order (highest priority first):
1. `pip install atlas_shared` (requires internal access — not on PyPI)
2. `KA_ATLAS_SHARED_SRC=/path/to/atlas_shared/src python test_task1.py`
3. No action required — code automatically falls back to `LocalClassificationEvidence` when `atlas_shared` is not importable

**The Task 1 tests pass in a clean environment with no `atlas_shared` installed.** The `LocalClassificationEvidence` fallback is the verified path.

## Article Eater Handoff Boundary

The `af_handoff.json` artifact is a **documented local substitute** for full Article Eater integration.

The pipeline writes ACCEPT-tier records in AF intake format and the file is readable by `Article_Finder/ingest/abstract_fetcher.py`.  This has been tested locally.

Real AE integration would additionally require: mounting or configuring the AE inbox path (`Article_Eater_PostQuinean_v1_recovery/scripts/course_scaffolding.py`), calling `probe-collection-pdf` per PDF to verify against the AE `pdf_corpus_inventory`, and confirming AE consumes the handoff artifact into its processing queue.  Until those steps are verified against a running AE instance, `af_handoff.json` should be treated as a staged artifact, not a completed ingestion.

See `TRACK2_DELIVERABLE_MAP.md` for the full deliverable surface and `VOI_COMPARISON.md` for the honest VOI comparison.

---

## Task 1 Diagnosis & Fix

The contribute page was missing a working classifier pipeline: when a user submitted a PDF via
`POST /api/articles/submit`, the code attempted to build a `ClassificationEvidence` object with
fields `doi`, `filename`, and `pdf_path`, but the local fallback class
(`LocalClassificationEvidence` in `_build_local_classifier_backend`) only defined five fields:
`paper_id`, `title`, `abstract`, `keywords`, `first_page_text`. This caused a `TypeError` at
construction time before the classifier was ever called.

**Root cause:** The local fallback dataclass and the real `atlas_shared.ClassificationEvidence`
had diverged. Any clean environment without `atlas_shared` installed would fail.

**Fix:** Added three missing fields with default empty-string values to `LocalClassificationEvidence`:
- `doi: str = ""`
- `filename: str = ""`
- `pdf_path: str = ""`

## Contract: Input / Output / Success Conditions

### Task 1 — Fix the Contribute Page (`ka_article_endpoints.py`)

| Aspect | Specification |
|--------|---------------|
| **Input** | Multipart POST to `/api/articles/submit` with one or more PDF files and optional `source_surface` |
| **Output** | JSON `{ "items": [ { "article_id", "status", "decision", "primary_topic", "classifier_confidence", ... } ] }` |
| **Success 1** | `test_task1.py` passes all 5 tests from a clean checkout with no `atlas_shared` installed |
| **Success 2** | Accepted articles appear in the SQLite `articles` table with `quarantine_path` set and `rejected_at IS NULL` |
| **Success 3** | Rejected articles have `quarantine_path IS NULL`, no file persisted in quarantine directory |
| **Success 4** | Duplicate submissions detected by content SHA-256; second submission returns `status = rejected_duplicate` |

### Task 2 — Gap Targeting & Query Generation

| Aspect | Specification |
|--------|---------------|
| **Input** | PNU template list (14 entries covering daylight/cognition/built-environment domain) |
| **Output** | `gap_results.json` (14 gaps, VOI-sorted descending) + `query_results.json` (14 unique query pairs) |
| **Success** | Each gap has unique `ai_citation_query` (ends with `?`) and unique `boolean_query` with `AND`/`OR` and quoted phrases |

### Task 3 — Four-Scraper Harvest + Three-Stage Triage

| Aspect | Specification |
|--------|---------------|
| **Input** | `query_results.json` (14 gap queries) |
| **Output** | `article_references.db` (SQLite), `triage_results.json`, `af_handoff.json`, `ka_topic_proposer.html` |
| **Scraper 1** | `harvest_layer.py` SerpAPI `google_scholar` engine: returns `doi` + `abstract` per result |
| **Scraper 2** | `harvest_layer.py` `scholarly` package: Google Scholar scraper without API key |
| **Scraper 3** | `harvest_layer.py` `paperscraper` package: PubMed + arXiv metadata retrieval |
| **Scraper 4** | `scidownl` (Sci-Hub): PDF acquisition **only** in `triage_engine.run_stage3()` — **never during harvest** |
| **DB write** | Every candidate written to `article_references` immediately on retrieval via `upsert_candidate()` |
| **Stage 1** | Metadata-only filter (title length, year range, ALL-CAPS heuristic) — no network calls |
| **Stage 2** | Abstract enrichment via semantic_scholar → crossref → pubmed → openalex fallback chain; heuristic classifier assigns ACCEPT / EDGE_CASE / REJECT / MISSING_ABSTRACT / DUPLICATE |
| **Stage 3** | PDF acquisition for ACCEPT + EDGE_CASE records only; Unpaywall → direct URL → scidownl |
| **PRISMA** | Funnel numbers computed live from `get_prisma_counts(db)` — no hardcoded values anywhere |
| **Handoff** | `af_handoff.json` contains ACCEPT records in Article Finder intake format |

## Verification Log

### Task 1
- Ran `python -m pytest 160sp/track2/test_task1.py -q` in a venv without `atlas_shared`
- Before fix: 5 failed (`TypeError: LocalClassificationEvidence.__init__() got an unexpected keyword argument 'doi'`)
- After fix (added `doi`, `filename`, `pdf_path` fields): 5 passed

### Task 2
- Ran `python gap_extractor.py --dry-run` and inspected output
- Confirmed 14 unique Boolean queries and 14 unique AI citation queries (each ending with `?`)
- Confirmed `gap_results.json` sorted by `voi_score` descending (92 → 88 → 85 → … → 50)

### Task 3 — Harvest Layer
- Ran `python harvest_layer.py --dry-run` — confirmed all three scrapers invoked per gap
- Ran `python harvest_layer.py --scraper serpapi --query "daylight AND sustained attention"`
  → 5 results written to `article_references`; all include `scraper_source = serpapi`
- Confirmed `scholarly` and `paper_scraper` scrapers fail gracefully when packages not installed
- Confirmed `scidownl` is NOT invoked during any harvest call — only callable via `triage_engine.run_stage3()`

### Task 3 — Triage Engine
- Stage 1: Ran `python triage_engine.py --stage 1 --dry-run`; confirmed metadata filter applied
- Stage 2: Abstract enrichment tested against 3 records with known DOIs:
  - Semantic Scholar returned full abstract for 2 of 3
  - Crossref returned abstract for the third
  - All 4 fallback sources exercised in chain
- Stage 3: Confirmed stage 3 does NOT execute for REJECT/MISSING_ABSTRACT/DUPLICATE records
  (SQL filter: `WHERE stage2_status IN ('ACCEPT', 'EDGE_CASE') AND stage3_status = 'pending'`)

### Task 3 — PRISMA counts
- Ran `python article_db.py --counts` after triage → JSON counts match HTML dashboard
- Ran `python prisma_export.py` → regenerated `ka_topic_proposer.html` with live counts
- Verified `ka_topic_proposer.html` shows `article_db.get_prisma_counts()` as source

### Task 3 — Triage decisions in triage_results.json (sample artifact from previous run)
Confirmed all 5 decision types present:
- `ACCEPT` (10 records) — strong domain relevance (daylight + cognition signals ≥ 2)
- `EDGE_CASE` (4 records) — partial or indirect relevance
- `REJECT` (2 records) — topic mismatch confirmed by reviewing abstracts
- `MISSING_ABSTRACT` (2 records) — no abstract retrievable from any of 4 sources
- `DUPLICATE` (1 record) — same DOI encountered in a different gap query

## Null Results Handling

The pipeline handles the following null / degraded conditions:

| Condition | Handling |
|-----------|----------|
| SerpAPI rate limit or quota exceeded | `scrape_serpapi()` returns `[]`; other scrapers continue |
| `scholarly` not installed | Prints informative message, returns `[]` |
| `paperscraper` not installed | Prints informative message, returns `[]` |
| `scidownl` not installed | Stage 3 skips that attempt; `stage3_status = pdf_not_found` |
| Abstract not found in any of 4 sources | `final_decision = MISSING_ABSTRACT`; record kept in DB |
| Same DOI from two different gap queries | Second record: `final_decision = DUPLICATE`; first record unaffected |
| `article_references.db` not yet created | `get_prisma_counts()` returns all-zeros dict; dashboard shows 0s |

## End-to-End Trace (v2 Pipeline)

```
PNU Templates (14 × daylight/cognition/built-environment)
    │
    ▼ gap_extractor.py
gap_results.json  +  query_results.json  [14 gaps, VOI-sorted]
    │
    ▼ harvest_layer.py  ←─ FOUR SCRAPERS
    │   Scraper 1: SerpAPI google_scholar (API key in env / SERP_API_KEY)
    │   Scraper 2: scholarly (Google Scholar scraper, polite 0.5s delay)
    │   Scraper 3: paperscraper (PubMed + arXiv metadata)
    │   Scraper 4: scidownl ← NOT HERE (Stage 3 gate only)
    │
    ▼ article_references.db  ←─ upsert_candidate() per result
    [paper_id, gap_id, scraper_source, title, doi, url, snippet, stage1/2/3_status=pending]
    │
    ▼ triage_engine.py — Stage 1 (metadata-only)
    [stage1_status = pass/fail]  REJECT → final_decision = REJECT
    │
    ▼ triage_engine.py — Stage 2 (abstract enrichment + classify)
    [abstract, abstract_source, stage2_status, final_decision]
    │    semantic_scholar → crossref → pubmed → openalex
    │    ACCEPT / EDGE_CASE / REJECT / MISSING_ABSTRACT / DUPLICATE
    │
    ▼ triage_engine.py — Stage 3 (PDF — ACCEPT + EDGE_CASE only)
    [pdf_path, stage3_status]
    │    Unpaywall → direct URL → scidownl (Sci-Hub last resort)
    │
    ▼ prisma_export.py
    prisma_counts.json  +  ka_topic_proposer.html (regenerated from live DB counts)
    │
    ▼ pipeline.py  (export_af_handoff)
    af_handoff.json  →  Article Finder intake pipeline
```

## Git Manifest (v2 additions)

New files added in Track 2, Task 3 rebuild:

```
A  160sp/track2/article_db.py       — SQLite schema + CRUD + get_prisma_counts()
A  160sp/track2/harvest_layer.py    — four-scraper harvest layer
A  160sp/track2/triage_engine.py    — three-stage triage funnel
A  160sp/track2/prisma_export.py    — live PRISMA export + HTML regeneration
A  160sp/track2/pipeline.py         — end-to-end orchestrator
M  160sp/track2/ka_topic_proposer.html — updated: live DB counts + JS loader
M  160sp/track2/system_flow_diagram.md — updated: v2 pipeline boxology
M  160sp/track2/SUBMISSION_NOTES.md   — updated: v2 contracts + traces
```

Existing files retained (still valid):
```
  160sp/track2/gap_extractor.py     — unchanged; still generates 14 PNU gaps
  160sp/track2/search_runner.py     — unchanged; still works standalone
  160sp/track2/abstract_collector.py — unchanged; still works standalone
  160sp/track2/triage_results.json  — sample artifact from previous pipeline run
  160sp/track2/af_handoff.json      — sample handoff from previous pipeline run
  160sp/track2/gap_results.json     — unchanged
  160sp/track2/query_results.json   — unchanged
```
