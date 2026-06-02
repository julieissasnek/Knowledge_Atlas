# System Flow Diagram — Track 2, Task 3 Pipeline (v2)

## Full Pipeline Boxology

```
┌─────────────────────────────────────────────────────────────────────────┐
│                  KNOWLEDGE ATLAS (KA)                                   │
│                                                                         │
│  ┌─────────────────┐     POST /api/articles/submit                     │
│  │  Contribute Page │ ──────────────────────────────────────────────►  │
│  │(ka_contribute_   │                                                   │
│  │  public.html)    │     ┌───────────────────────────────────────┐    │
│  └─────────────────┘     │  ka_article_endpoints.py               │    │
│                           │  1. PDF Validation (size, SHA-256)     │    │
│                           │  2. Duplicate Detection (SHA-256 index)│    │
│                           │  3. ClassificationEvidence build       │    │
│                           │  4. AdaptiveClassifier (atlas_shared   │    │
│                           │     or LocalClassificationEvidence)    │    │
│                           │  5. SQLite articles + audit_log        │    │
│                           └───────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────────┘
                                      │
                                      │ ACCEPT → quarantine
                                      ▼
┌─────────────────────────────────────────────────────────────────────────┐
│              TRACK 2, TASK 3 — FOUR-SCRAPER RETRIEVAL PIPELINE          │
│                                                                         │
│  ┌──────────────────┐                                                  │
│  │ gap_extractor.py │  14 PNU templates (daylight/cognition domain)    │
│  │                  │  VOI-scored, sorted highest→lowest               │
│  └────────┬─────────┘                                                  │
│           │ gap_results.json  +  query_results.json                    │
│           ▼                                                             │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │                     harvest_layer.py                             │  │
│  │                                                                  │  │
│  │  Scraper 1: SerpAPI/google_scholar  ─────────────────────────►  │  │
│  │  Scraper 2: scholarly (Google Scholar scraper, no API key)  ──► │  │
│  │  Scraper 3: paper-scraper (PubMed + arXiv metadata)         ──► │  │
│  │  Scraper 4: scidownl ── PDF ACQUISITION ONLY (Stage 3 gate) ──  │  │
│  │            (scidownl is NEVER called during harvest)            │  │
│  │                                                                  │  │
│  │  Every candidate → upsert_candidate() → article_references.db  │  │
│  └────────────────────────────┬─────────────────────────────────────┘  │
│                               │                                         │
│           ┌───────────────────▼──────────────────────────┐             │
│           │         article_references.db (SQLite)        │             │
│           │  paper_id · gap_id · scraper_source           │             │
│           │  title · doi · url · authors · year · venue   │             │
│           │  snippet · abstract · abstract_source         │             │
│           │  pdf_path                                      │             │
│           │  stage1_status · stage2_status · stage3_status│             │
│           │  final_decision · created_at · updated_at     │             │
│           └───────────────────┬──────────────────────────┘             │
│                               │                                         │
│           ┌───────────────────▼──────────────────────────┐             │
│           │           triage_engine.py                    │             │
│           │                                               │             │
│           │  STAGE 1: Metadata-only (no network)          │             │
│           │    title < 5 chars?         → REJECT          │             │
│           │    year ∉ [1990–2030]?      → REJECT          │             │
│           │    ALL-CAPS title?          → REJECT          │             │
│           │    else                     → pass to Stage 2  │             │
│           │                                               │             │
│           │  STAGE 2: Abstract enrichment + classify      │             │
│           │    fallback chain:                            │             │
│           │      semantic_scholar → crossref              │             │
│           │      → pubmed → openalex                      │             │
│           │    heuristic domain classifier:               │             │
│           │      strong signals ≥ 2  → ACCEPT             │             │
│           │      strong ≥ 1 OR weak ≥ 3 → EDGE_CASE      │             │
│           │      < 20 words abstract  → MISSING_ABSTRACT  │             │
│           │      duplicate DOI         → DUPLICATE        │             │
│           │      else                 → REJECT            │             │
│           │                                               │             │
│           │  STAGE 3: PDF acquisition (ACCEPT+EDGE only)  │             │
│           │  ┌──────────────────────────────────────────┐ │             │
│           │  │ *** scidownl ONLY CALLED HERE ***        │ │             │
│           │  │  1. Unpaywall (open-access OA PDF URL)   │ │             │
│           │  │  2. Direct URL download (.pdf extension) │ │             │
│           │  │  3. scidownl/Sci-Hub  ← last resort      │ │             │
│           │  └──────────────────────────────────────────┘ │             │
│           └───────────────────┬──────────────────────────┘             │
│                               │                                         │
│           ┌───────────────────▼──────────────────────────┐             │
│           │            prisma_export.py                   │             │
│           │                                               │             │
│           │  get_prisma_counts(db) ← live DB counts       │             │
│           │  → prisma_counts.json                         │             │
│           │  → ka_topic_proposer.html (regenerated)       │             │
│           └───────────────────┬──────────────────────────┘             │
│                               │                                         │
│           ┌───────────────────▼──────────────────────────┐             │
│           │  af_handoff.json (Article Finder boundary)    │             │
│           │  ACCEPT records only → AF intake              │             │
│           └───────────────────────────────────────────────┘             │
└─────────────────────────────────────────────────────────────────────────┘
```

## File Inventory

| File | Role |
|------|------|
| `gap_extractor.py` | Generates 14 PNU gaps + boolean/AI-citation queries |
| `gap_results.json` | Gap records with VOI scores |
| `query_results.json` | Boolean + AI-citation query pairs per gap |
| `harvest_layer.py` | **NEW** Four-scraper harvest (SerpAPI, scholarly, paper-scraper, scidownl gate) |
| `article_db.py` | **NEW** SQLite schema, CRUD helpers, `get_prisma_counts()` |
| `article_references.db` | **NEW** Master candidate table — written by harvest, updated by triage |
| `triage_engine.py` | **NEW** Three-stage triage funnel (replaces abstract_collector.py) |
| `prisma_export.py` | **NEW** Reads live DB counts, regenerates PRISMA HTML |
| `pipeline.py` | **NEW** End-to-end orchestrator |
| `search_runner.py` | Legacy SerpAPI runner (still works standalone) |
| `abstract_collector.py` | Legacy abstract enricher (still works standalone) |
| `triage_results.json` | Legacy triage output (still valid as sample artifact) |
| `ka_topic_proposer.html` | PRISMA dashboard (regenerated by prisma_export.py) |
| `ka_contribute_public.html` | Article submission UI |
| `af_handoff.json` | Article Finder boundary artifact (ACCEPT records) |
| `SUBMISSION_NOTES.md` | Contract, trace, verification |

## Key Design Invariants

1. **Every candidate is written to `article_references` first** — no processing before DB insert
2. **PDFs are never fetched before Stage 2 clears a record** — scidownl only callable from `triage_engine.run_stage3()`
3. **PRISMA numbers always come from live DB** — `get_prisma_counts()` is the single source of truth
4. **Scrapers degrade gracefully** — if `scholarly` or `paperscraper` not installed, that scraper is skipped; SerpAPI results still written
5. **All four scraper sources tagged** — `scraper_source` column distinguishes `serpapi / scholarly / paper_scraper_pubmed / paper_scraper_arxiv`

## PRISMA Decision Decision Tree

```
article_references row
    │
    ├─ Stage 1: title < 5 chars?          ──► stage1_status=fail → REJECT
    │          year ∉ [1990–2030]?        ──► stage1_status=fail → REJECT
    │          ALL-CAPS title?            ──► stage1_status=fail → REJECT
    │          else                       ──► stage1_status=pass
    │
    ├─ Stage 2: DOI already seen?         ──► DUPLICATE
    │          abstract < 20 words?       ──► MISSING_ABSTRACT
    │          strong signals ≥ 2         ──► ACCEPT  ─┐
    │          strong ≥ 1 OR weak ≥ 3     ──► EDGE_CASE─┤→ proceed to Stage 3
    │          else                       ──► REJECT    │
    │                                                   │
    └─ Stage 3 (ACCEPT + EDGE_CASE only)               │
               Unpaywall URL found?       ──► pdf_found ◄─┘
               Direct .pdf URL works?    ──► pdf_found
               scidownl succeeds?        ──► pdf_found
               all fail                  ──► pdf_not_found
```
