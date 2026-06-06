# PHASE 7 COMPLIANCE REPORT — Track 2 Task 3
**Generated:** 2026-06-06 00:58 UTC
**Database:** `pipeline_lifecycle_full.db`
**Source run:** `article_references.db` — Gemini legacy harvest, 2026-06-02

---

## STEP 1: PIPELINE EXECUTION SUMMARY

The legacy `article_references.db` (88 papers, 14 gaps, harvested via SerpAPI
google_scholar engine on 2026-06-02) was migrated into `pipeline_lifecycle_full.db`
and processed through all phases (Phase 3 ingest through Phase 4D classification).

| Phase | Module | Outcome |
|-------|--------|---------|
| Phase 3 | `write_candidates_to_lifecycle()` | 88 inserted, 0 duplicates, 0 errors |
| Phase 4A | `run_phase4a_metadata_gate()` | 69 passed, 19 rejected (21.6% noise removed) |
| Phase 4B | Abstract backfill from legacy run | 47 abstracts populated, 22 missing |
| Phase 4D | `run_phase4d_triage()` | ACCEPT=7, EDGE=7, REJECT=33 |

---

## STEP 2: END-TO-END CANDIDATE JOURNAL TRACE

*One paper traced from query to triage decision — highest VOI ACCEPT record.*

**Gap source:** `GAP-PNU-001`
**Boolean query:** `"daylight exposure" AND "sustained attention" AND ("open-plan" OR "open plan")`
**Title:** Exploring the impact of external shading system on cognitive task performance, alertness and visual comfort in a daylit workplace environment
**DOI:** `(not available)`
**Reference ID:** `REF-2026-06-06-000002`
**Discovered via:** `serpapi_scholar`
**Publication year:** 2020
**Venue:** (not recorded)

**Abstract source:** `semantic_scholar`
**Abstract (first 120 chars):** The authors examined the effect of external shading system on cognitive performance, alertness and visual comfort of vis...

**Phase 4A confidence:** `1.0`
**Phase 4A outcome:** `accepted_for_download`
**Phase 4D topic confidence:** `1.0`
**Phase 4D VOI score:** `0.83`
**Phase 4D decision:** `ACCEPT`
**Triage reason:** Abstract strongly matches target topic (confidence 1.00) and VOI score 0.83 exceeds acceptance threshold 0.7.

**Stored at:** `article_references` row `REF-2026-06-06-000002` in `pipeline_lifecycle_full.db`

---

## STEP 3: HIGH-VOI NULL RESULTS LEDGER

**0** of 14 gaps returned **zero papers** across all scrapers.

All 14 gaps returned at least one paper. No null-result gaps detected.

---

## STEP 4: MISSING_ABSTRACT METRIC AUDIT

**Papers with MISSING_ABSTRACT: 5 out of 88 total records.**

These records exhausted the Phase 4B fallback chain (Semantic Scholar → CrossRef → PubMed → OpenAlex)
without retrieving a full abstract. They were **not** scored by the topic classifier or VOI function.
Their `triage_stage = 'abstract_missing'` distinguishes them from domain-REJECT papers.

| Reference ID | Title (truncated) | Gap | DOI |
|---|---|---|---|
| `REF-2026-06-06-000005` | … ARCHITECTURAL LIGHTING DESIGN TO ENHANCE INDIVIDUALS' | `GAP-PNU-001` | `(none)` |
| `REF-2026-06-06-000058` | OPTIMIZING DAYLIGHT AND AESTHETICS IN ENERGY RENOVATION | `GAP-PNU-013` | `(none)` |
| `REF-2026-06-06-000064` | The Impact of Biophilic Design in School Common Areas o | `GAP-PNU-002` | `(none)` |
| `REF-2026-06-06-000065` | Bracing Biophilia: When biophilic design promotes pupil | `GAP-PNU-002` | `(none)` |
| `REF-2026-06-06-000066` | Effects of Colour Temperature in Classroom Lighting on  | `GAP-PNU-003` | `(none)` |

**Verification:** `triage_stage = 'abstract_missing'` rows have NULL `abstract` column,
NULL `phase4d_decision`, NULL `phase4d_voi_score`. They are stored in `article_references`
and surfaced by `get_unobtainable_candidates()`. They are NOT silently dropped.

---

## PRISMA FUNNEL TABLE (live from database)

| Funnel Stage | Count | Source |
|---|---|---|
| Gaps targeted (from Task 2) | **14** | `len(query_results.json) = 14` |
| Queries executed (SerpAPI google_scholar) | **14** | `COUNT(DISTINCT discovered_query) WHERE discovered_via='serpapi_sc` |
| Records returned | **88** | `COUNT(*) FROM article_references` |
| &#8594;&nbsp;Duplicates removed | **0** | `COUNT(*) WHERE triage_stage = 'duplicate'` |
| Abstracts collected | **35** | `COUNT(*) WHERE abstract IS NOT NULL AND LENGTH(TRIM(abstract)) > ` |
| &#8594;&nbsp;MISSING_ABSTRACT (no abstract found) | **22** | `COUNT(*) WHERE triage_stage='abstract_missing' OR phase4d_decisio` |
| Screened by classifier (Phase 4D input) | **47** | `COUNT(*) WHERE phase4d_decision IS NOT NULL AND phase4d_decision ` |
| &#8594;&nbsp;→ ACCEPT (on-topic, high VOI) | **7** | `COUNT(*) WHERE phase4d_decision = 'ACCEPT'` |
| &#8594;&nbsp;→ EDGE_CASE (borderline) | **7** | `COUNT(*) WHERE phase4d_decision = 'EDGE_CASE'` |
| &#8594;&nbsp;→ REJECT (off-topic) | **33** | `COUNT(*) WHERE phase4d_decision = 'REJECT'` |

**Conservation Law 1** (Records - Duplicates = Abstracts + MISSING_ABSTRACT):
- Left side: 88  Right side: 57  Delta: +31
- **Status: ALERT (delta=+31, attributed to intermediate pipeline stages)**

**Conservation Law 2** (Abstracts = ACCEPT + EDGE_CASE + REJECT):
- Left side: 35  Right side: 47  Delta: -12
- **Status: ALERT (delta=-12)**

---

## FULL STAGE DISTRIBUTION (raw counts from `article_references`)

| triage_stage | Count |
|---|---|
| `rejected_at_abstract` | 33 |
| `abstract_missing` | 22 |
| `rejected_at_metadata` | 19 |
| `accepted_for_download` | 7 |
| `edge_case_review` | 7 |

## GAP COVERAGE

| Gap ID | Total Papers | ACCEPT count |
|---|---|---|
| `GAP-PNU-001` | 5 | 1 |
| `GAP-PNU-002` | 7 | 1 |
| `GAP-PNU-003` | 7 | 1 |
| `GAP-PNU-004` | 5 | 0 |
| `GAP-PNU-005` | 6 | 1 |
| `GAP-PNU-006` | 7 | 1 |
| `GAP-PNU-007` | 4 | 0 |
| `GAP-PNU-008` | 10 | 0 |
| `GAP-PNU-009` | 6 | 0 |
| `GAP-PNU-010` | 7 | 0 |
| `GAP-PNU-011` | 5 | 0 |
| `GAP-PNU-012` | 7 | 1 |
| `GAP-PNU-013` | 7 | 0 |
| `GAP-PNU-014` | 5 | 1 |

## ABSTRACT SOURCE BREAKDOWN

| Source | Count |
|---|---|
| `crossref` | 15 |
| `snippet` | 12 |
| `openalex` | 11 |
| `pubmed` | 7 |
| `semantic_scholar` | 2 |