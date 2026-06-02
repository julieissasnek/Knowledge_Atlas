# System Flow Diagram — Track 2 Article Retrieval Pipeline

## Pipeline Boxology

```
┌─────────────────────────────────────────────────────────────────┐
│                   KNOWLEDGE ATLAS (KA)                          │
│                                                                 │
│  ┌─────────────────┐     POST /api/articles/submit             │
│  │  Contribute Page │ ──────────────────────────────────────►  │
│  │(ka_contribute_   │                                          │
│  │  public.html)    │     ┌──────────────────────────────────┐ │
│  └─────────────────┘     │  ka_article_endpoints.py          │ │
│                           │  ┌────────────────────────────┐  │ │
│                           │  │ 1. PDF Validation           │  │ │
│                           │  │    (size, format, SHA-256)  │  │ │
│                           │  └────────────┬───────────────┘  │ │
│                           │               │                   │ │
│                           │  ┌────────────▼───────────────┐  │ │
│                           │  │ 2. Duplicate Detection      │  │ │
│                           │  │    (content SHA-256 index)  │  │ │
│                           │  └────────────┬───────────────┘  │ │
│                           │               │                   │ │
│                           │  ┌────────────▼───────────────┐  │ │
│                           │  │ 3. ClassificationEvidence   │  │ │
│                           │  │    construction             │  │ │
│                           │  │  (paper_id, doi, filename,  │  │ │
│                           │  │   pdf_path)                 │  │ │
│                           │  └────────────┬───────────────┘  │ │
│                           │               │                   │ │
│                           │  ┌────────────▼───────────────┐  │ │
│                           │  │ 4. AdaptiveClassifier       │  │ │
│                           │  │    (atlas_shared or local   │  │ │
│                           │  │     fallback)               │  │ │
│                           │  └────────────┬───────────────┘  │ │
│                           │               │ ACCEPT/EDGE/REJECT│ │
│                           │  ┌────────────▼───────────────┐  │ │
│                           │  │ 5. SQLite articles table    │  │ │
│                           │  │    + audit_log              │  │ │
│                           │  └────────────────────────────┘  │ │
│                           └──────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
                                      │
                                      │ ACCEPT → quarantine
                                      ▼
┌─────────────────────────────────────────────────────────────────┐
│              TRACK 2 RETRIEVAL PIPELINE                         │
│                                                                 │
│  ┌─────────────────┐                                           │
│  │ gap_extractor.py │  14 PNU templates (daylight/cognition)   │
│  │                  │  VOI-scored, domain-specific             │
│  └────────┬─────────┘                                          │
│           │ gap_results.json  +  query_results.json            │
│           ▼                                                     │
│  ┌─────────────────┐                                           │
│  │ search_runner.py │  SerpAPI → google_scholar engine         │
│  │                  │  Returns: title, snippet, doi, url       │
│  └────────┬─────────┘                                          │
│           │ search_results.json                                │
│           ▼                                                     │
│  ┌──────────────────────┐                                      │
│  │ abstract_collector.py │  Fallback chain:                    │
│  │                       │  1. semantic_scholar (DOI/title)    │
│  │                       │  2. crossref (DOI registry)         │
│  │                       │  3. pubmed (biomedical)             │
│  │                       │  4. openalex (open scholarly graph) │
│  └────────┬──────────────┘                                     │
│           │ triage_results.json                                │
│           │ (doi + abstract + decision per record)             │
│           ▼                                                     │
│  ┌──────────────────┐                                          │
│  │ af_handoff.json  │  Article Finder boundary artifact        │
│  │                  │  ACCEPT records only → AF intake         │
│  └──────────────────┘                                          │
└─────────────────────────────────────────────────────────────────┘
```

## Decision Logic

```
triage record
    │
    ├─ DOI already seen in session? ──► DUPLICATE
    │
    ├─ abstract null or < 20 words? ──► MISSING_ABSTRACT
    │
    ├─ strong domain signals ≥ 2?   ──► ACCEPT
    │   (daylight, circadian, cognitive, classroom, etc.)
    │
    ├─ strong signal ≥ 1 OR
    │   weak signals ≥ 3?           ──► EDGE_CASE
    │
    └─ otherwise                    ──► REJECT
```
