# Track 2 Deliverable Map

**Student:** Julie Issasnek  
**Course:** COGS 160 — Track 2, Tasks 1–3  
**Branch:** `track2/julie-issasnek`  
**Date:** 2026-06-04

This file tells a grader or reviewer exactly which files are the grading surface, which are supporting infrastructure, and which are inherited scaffolding that was not written for this assignment.

---

## Core Grading Surface

These files ARE the Track 2 deliverable.  A grader should evaluate these.

| File | Task | What it does |
|------|------|-------------|
| `gap_extractor.py` | Task 2 | Extracts 14 PNU gaps from daylight/cognition domain; assigns VOI priority scores; writes `gap_results.json` + `query_results.json` |
| `harvest_layer.py` | Task 3 | Four-scraper harvest layer (SerpAPI primary, scholarly + paperscraper fallbacks, scidownl PDF-only); writes all candidates to `article_references` via `upsert_candidate()` |
| `search_runner.py` | Task 3 | CLI entry point for SerpAPI; delegates to `harvest_layer.scrape_serpapi()`; supports ad-hoc queries + batch mode |
| `triage_engine.py` | Task 3 | Three-stage triage funnel: Stage 1 metadata filter → Stage 2 abstract enrichment + heuristic classify → Stage 3 PDF acquisition; SQL gate enforces Stage 2 must precede Stage 3 |
| `abstract_collector.py` | Task 3 | Standalone abstract fallback chain: Semantic Scholar → CrossRef → PubMed → OpenAlex; used by Stage 2 |
| `article_db.py` | Task 3 | SQLite schema + CRUD for `article_references`; `get_prisma_counts()` returns live counts from DB (never hardcoded) |
| `prisma_export.py` | Task 3 | Exports `prisma_counts.json` and regenerates `ka_topic_proposer.html` with live DB counts |
| `pipeline.py` | Task 3 | End-to-end orchestrator: gap_extractor → harvest → triage (S1+S2+S3) → prisma_export → af_handoff |
| `test_task1.py` | Task 1 | Pytest suite for `ka_article_endpoints.py` — uses `tmp_path` isolation |
| `test_track2_task3.py` | Task 3 | Pytest suite for Task 3 pipeline — each test uses temp SQLite DB; no shared state |
| `ka_topic_proposer.html` | Task 3 | PRISMA dashboard — counts loaded from `prisma_counts.json` at runtime; no hardcoded numbers |

---

## Supporting Documents

These files explain and contextualise the core grading surface.  Reviewers should read them but they are not code deliverables.

| File | Purpose |
|------|---------|
| `SUBMISSION_NOTES.md` | Full pipeline contract: input/output specs, success conditions, verification log, end-to-end trace |
| `VOI_COMPARISON.md` | Honest comparison of Track 2 heuristic VOI vs Article Eater/BN structural+epistemic VOI machinery |
| `TRACK2_DELIVERABLE_MAP.md` | This file |
| `system_flow_diagram.md` | ASCII boxology of the pipeline; matches the end-to-end trace in SUBMISSION_NOTES.md |
| `spot_check.txt` | Six manual spot-check records verifying SerpAPI, abstract chain, DB schema, scraper isolation, duplicate detection, and live PRISMA counts |
| `trace_log.txt` | Execution trace of a full pipeline run; used to verify log output matches code |

---

## Generated Outputs (Regenerable Artifacts)

These files are produced by running the pipeline.  They are committed as evidence of a successful run, not as permanent code.  A grader can verify them by running `py pipeline.py --skip-stage3` (see SUBMISSION_NOTES.md).

| File | How to regenerate |
|------|-----------------|
| `gap_results.json` | `py gap_extractor.py` |
| `query_results.json` | `py gap_extractor.py` |
| `article_references.db` | `py pipeline.py` (full run) |
| `triage_results.json` | `py pipeline.py` (full run) |
| `af_handoff.json` | `py pipeline.py` (full run) |
| `prisma_counts.json` | `py prisma_export.py` |
| `ka_topic_proposer.html` | `py prisma_export.py` |

---

## Inherited / Unmodified Scaffolding

These files were provided by course infrastructure, not written for this submission.  Task 1 required modifying `ka_article_endpoints.py`; the others were already present.

| File | Origin | Modified? |
|------|--------|-----------|
| `ka_article_endpoints.py` | Course scaffold | ✅ Yes — added 3 missing fields to `LocalClassificationEvidence` (Task 1 fix) |
| `ka_contribute_public.html` | Course scaffold | Minor — updated PRISMA note |
| `_init_.py` | Course scaffold | No |

---

## Dependency Map

### Task 1 (`ka_article_endpoints.py`)

```
fastapi           — pip install fastapi
pytest            — pip install pytest
httpx             — pip install httpx  (needed by TestClient)
atlas_shared      — OPTIONAL fallback: LocalClassificationEvidence activates when absent
```

`atlas_shared` resolution order (highest priority first):
1. `pip install atlas_shared` (not available on PyPI — requires internal access)
2. Set `KA_ATLAS_SHARED_SRC=/path/to/atlas_shared/src` before running
3. Automatic — code falls back to `LocalClassificationEvidence` if import fails

**The Task 1 test suite (`test_task1.py`) passes in a clean environment with NO `atlas_shared` installed.**  The fallback is the tested path.

### Task 3 Pipeline (all pipeline files)

```
requests          — pip install requests       (required)
scholarly         — pip install scholarly      (optional: Google Scholar fallback scraper)
paperscraper      — pip install paperscraper   (optional: PubMed + arXiv)
scidownl          — pip install scidownl       (optional: PDF acquisition last resort)
```

**The Task 3 pipeline is fully standalone.** No `atlas_shared` dependency.  `scholarly`, `paperscraper`, and `scidownl` all degrade gracefully if absent.

---

## Verification Commands (Sequential — Do Not Run in Parallel)

> ⚠️ Run these sequentially.  The test suite uses isolated temp databases, but two concurrent pytest processes targeting the same `article_references.db` could conflict.  The pipeline commands write to `article_references.db` — run one at a time.

### Task 1
```bash
py -3.14 -m pytest test_task1.py -v
# Expected: 5 passed
```

### Task 3 (offline, no API key needed)
```bash
py -3.14 -m pytest test_track2_task3.py -v
# Expected: 20+ passed, 0 failed
```

### Task 2 + 3 pipeline (requires SERP_API_KEY for live SerpAPI calls)
```bash
# Step 1: generate gaps + queries
py -3.14 gap_extractor.py

# Step 2: harvest (SerpAPI only; ~90 seconds due to rate limiting)
py -3.14 harvest_layer.py --scraper serpapi --num 5

# Step 3: triage + PRISMA export (no live PDFs)
py -3.14 pipeline.py --skip-harvest --skip-stage3

# Step 4: verify PRISMA counts
py -3.14 article_db.py --counts
```

### Reset command (if DB state is corrupted)
```bash
del article_references.db && py -3.14 article_db.py --init --db article_references.db
```

---

## Article Eater Handoff Boundary

The `af_handoff.json` artifact is a **local substitute** for full Article Eater integration.

The pipeline writes ACCEPT-tier records in AF intake format.  What the current code does:
- Writes `af_handoff.json` with `paper_id`, `title`, `abstract`, `doi`, `source_gap`, `triage_decision`
- The file is readable by `Article_Finder/ingest/abstract_fetcher.py`

What real Article Eater integration would additionally require:
- Mount or configure the AE inbox path (`Article_Eater_PostQuinean_v1_recovery/scripts/course_scaffolding.py`)
- Call `course_scaffolding.py probe-collection-pdf` for each PDF to verify against the AE corpus inventory
- Verify that AE consumes the handoff artifact into its queue or `pdf_corpus_inventory` table
- Confirm `ae_waiting_room_probe.probe_pdf_against_article_eater()` returns non-None responses

Until those steps are verified, `af_handoff.json` should be treated as a staged artifact, not a completed ingestion.
