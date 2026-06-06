"""
prisma_dashboard_data.py -- Phase 6 PRISMA dashboard data aggregator.

Single source of truth for all six dashboard panels.  Reads exclusively
from pipeline_lifecycle_full.db via lifecycle_db.get_connection() and from
query_results.json for gap-level metadata.  Zero hardcoded numbers.

All public functions return plain dicts so they can be JSON-serialised
and re-used by the HTML generator, the API route, and the test suite.

Panel layout
------------
  A: Gap Summary           -- total gaps, top-5 by VOI score
  B: Search Summary        -- boolean query count, raw result count
  C: Abstract Telemetry    -- collected vs MISSING_ABSTRACT
  D: Triage Results        -- ACCEPT / EDGE_CASE / REJECT live counts
  E: PRISMA Funnel         -- Identification -> Screening -> Eligibility -> Inclusion
  F: Null Results Ledger   -- gaps that returned zero papers
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from lifecycle_db import LIFECYCLE_DB, get_connection

DEFAULT_QUERY_FILE = str(
    Path(__file__).resolve().parent / "query_results.json"
)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _conn(db_path: str):
    """Return a lifecycle_db connection, or None if DB doesn't exist."""
    if not Path(db_path).exists() or Path(db_path).stat().st_size == 0:
        return None
    return get_connection(db_path)


def _scalar(conn, sql: str, params: tuple = ()) -> int:
    """Execute a scalar COUNT query and return the integer result."""
    row = conn.execute(sql, params).fetchone()
    return row[0] if row and row[0] is not None else 0


def _load_query_results(query_file: str) -> list[dict]:
    """Load gap query definitions from query_results.json."""
    p = Path(query_file)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


# ── Panel A: Gap Summary ──────────────────────────────────────────────────────

def compute_panel_a(
    db_path: str = LIFECYCLE_DB,
    query_file: str = DEFAULT_QUERY_FILE,
) -> dict:
    """
    Panel A: Gap Summary.

    Returns:
      total_gaps        -- total number of distinct research gaps defined
      gaps_with_results -- gaps that produced at least one row in article_references
      top5_by_voi       -- top-5 gaps ranked by max(phase4d_voi_score) DESC
                          Each entry: {gap_id, boolean_query, total_papers,
                                       accept_count, max_voi_score, avg_voi_score}
    """
    gaps = _load_query_results(query_file)
    total_gaps = len(gaps)
    gap_query_map = {g["gap_id"]: g.get("boolean_query", "") for g in gaps}

    conn = _conn(db_path)
    if conn is None:
        return {
            "total_gaps": total_gaps,
            "gaps_with_results": 0,
            "top5_by_voi": [],
        }

    with conn:
        rows = conn.execute(
            """
            SELECT
                discovered_query                                            AS gap_id,
                COUNT(*)                                                    AS total_papers,
                SUM(CASE WHEN phase4d_decision='ACCEPT'    THEN 1 ELSE 0 END) AS accept_count,
                SUM(CASE WHEN phase4d_decision='EDGE_CASE' THEN 1 ELSE 0 END) AS edge_count,
                MAX(phase4d_voi_score)                                      AS max_voi_score,
                AVG(CASE WHEN phase4d_decision='ACCEPT'
                         THEN phase4d_voi_score END)                        AS avg_voi_score
            FROM article_references
            WHERE discovered_query LIKE 'GAP-%'
            GROUP BY discovered_query
            ORDER BY max_voi_score DESC NULLS LAST
            LIMIT 5
            """
        ).fetchall()

        gaps_with_results_n = _scalar(
            conn,
            "SELECT COUNT(DISTINCT discovered_query) FROM article_references "
            "WHERE discovered_query LIKE 'GAP-%'",
        )

    top5 = []
    for r in rows:
        gid = r["gap_id"] or ""
        top5.append({
            "gap_id":        gid,
            "boolean_query": gap_query_map.get(gid, ""),
            "total_papers":  r["total_papers"] or 0,
            "accept_count":  r["accept_count"] or 0,
            "edge_count":    r["edge_count"]   or 0,
            "max_voi_score": round(r["max_voi_score"], 4) if r["max_voi_score"] else None,
            "avg_voi_score": round(r["avg_voi_score"], 4) if r["avg_voi_score"] else None,
        })

    return {
        "total_gaps":        total_gaps,
        "gaps_with_results": gaps_with_results_n,
        "top5_by_voi":       top5,
    }


# ── Panel B: Search Summary ───────────────────────────────────────────────────

def compute_panel_b(
    db_path: str = LIFECYCLE_DB,
    query_file: str = DEFAULT_QUERY_FILE,
) -> dict:
    """
    Panel B: Search Summary.

    Returns:
      total_boolean_queries -- number of gap queries defined in query_results.json
      gaps_that_returned_results -- gaps with >= 1 row in article_references
      total_raw_results     -- COUNT(*) from article_references (all rows)
      scrapers_used         -- distinct scraper_source values seen
    """
    gaps = _load_query_results(query_file)
    total_queries = len(gaps)

    conn = _conn(db_path)
    if conn is None:
        return {
            "total_boolean_queries":       total_queries,
            "gaps_that_returned_results":  0,
            "total_raw_results":           0,
            "scrapers_used":               [],
        }

    with conn:
        total_raw   = _scalar(conn, "SELECT COUNT(*) FROM article_references")
        gaps_active = _scalar(
            conn,
            "SELECT COUNT(DISTINCT discovered_query) FROM article_references "
            "WHERE discovered_query LIKE 'GAP-%'",
        )
        scraper_rows = conn.execute(
            "SELECT DISTINCT discovered_via FROM article_references "
            "WHERE discovered_via IS NOT NULL AND discovered_via != '' "
            "ORDER BY discovered_via"
        ).fetchall()

    return {
        "total_boolean_queries":      total_queries,
        "gaps_that_returned_results": gaps_active,
        "total_raw_results":          total_raw,
        "scrapers_used":              [r["discovered_via"] for r in scraper_rows],
    }


# ── Panel C: Abstract Collection Telemetry ────────────────────────────────────

def compute_panel_c(db_path: str = LIFECYCLE_DB) -> dict:
    """
    Panel C: Abstract Collection Telemetry.

    Returns:
      abstracts_collected   -- rows where abstract column is non-empty (> 50 chars)
      missing_abstract      -- rows with triage_stage='abstract_missing'
                               OR phase4d_decision='MISSING_ABSTRACT'
      abstract_source_breakdown -- dict of source_name -> count
    """
    conn = _conn(db_path)
    if conn is None:
        return {"abstracts_collected": 0, "missing_abstract": 0,
                "abstract_source_breakdown": {}}

    with conn:
        collected = _scalar(
            conn,
            "SELECT COUNT(*) FROM article_references "
            "WHERE abstract IS NOT NULL AND LENGTH(TRIM(abstract)) > 50",
        )
        missing = _scalar(
            conn,
            "SELECT COUNT(*) FROM article_references "
            "WHERE triage_stage='abstract_missing' "
            "   OR phase4d_decision='MISSING_ABSTRACT'",
        )
        src_rows = conn.execute(
            "SELECT abstract_source, COUNT(*) AS n FROM article_references "
            "WHERE abstract_source IS NOT NULL AND abstract_source != '' "
            "GROUP BY abstract_source ORDER BY n DESC"
        ).fetchall()

    return {
        "abstracts_collected": collected,
        "missing_abstract":    missing,
        "abstract_source_breakdown": {r["abstract_source"]: r["n"] for r in src_rows},
    }


# ── Panel D: Triage Results ───────────────────────────────────────────────────

def compute_panel_d(db_path: str = LIFECYCLE_DB) -> dict:
    """
    Panel D: Triage Results.

    Reads phase4d_decision from article_references in pipeline_lifecycle_full.db.
    Returns live counts for ACCEPT, EDGE_CASE, REJECT, MISSING_ABSTRACT.
    """
    conn = _conn(db_path)
    if conn is None:
        return {"accept": 0, "edge_case": 0, "reject": 0, "missing_abstract": 0}

    with conn:
        row = conn.execute(
            """
            SELECT
                SUM(CASE WHEN phase4d_decision='ACCEPT'           THEN 1 ELSE 0 END) AS accept,
                SUM(CASE WHEN phase4d_decision='EDGE_CASE'        THEN 1 ELSE 0 END) AS edge_case,
                SUM(CASE WHEN phase4d_decision='REJECT'           THEN 1 ELSE 0 END) AS reject,
                SUM(CASE WHEN phase4d_decision='MISSING_ABSTRACT' THEN 1 ELSE 0 END) AS missing_abstract
            FROM article_references
            """
        ).fetchone()

    return {
        "accept":           int(row["accept"] or 0),
        "edge_case":        int(row["edge_case"] or 0),
        "reject":           int(row["reject"] or 0),
        "missing_abstract": int(row["missing_abstract"] or 0),
    }


# ── Panel E: PRISMA Funnel ────────────────────────────────────────────────────

def compute_panel_e(db_path: str = LIFECYCLE_DB) -> dict:
    """
    Panel E: Complete PRISMA funnel counts.

    Identification  -> total rows in article_references
    Deduplication   -> rows with triage_stage='duplicate'
    Metadata screen -> rows with triage_stage='rejected_at_metadata'
    Screened        -> identified - duplicates - metadata_rejected
    Abstract phase  -> screened rows that reached abstract collection
    Eligibility     -> rows that passed Phase 4D (phase4d_decision set)
    Inclusion       -> PDFs acquired (rows in papers table)

    All numbers are consistent: screened = identified - duplicates.
    """
    conn = _conn(db_path)
    if conn is None:
        return {
            "identified": 0, "duplicates": 0, "metadata_rejected": 0,
            "screened": 0, "abstracts_retrieved": 0, "abstract_missing": 0,
            "accept": 0, "edge_case": 0, "reject": 0,
            "pdfs_acquired": 0, "unobtainable": 0,
        }

    with conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*)                                                                AS identified,
                SUM(CASE WHEN triage_stage='duplicate'            THEN 1 ELSE 0 END)  AS duplicates,
                SUM(CASE WHEN triage_stage='rejected_at_metadata' THEN 1 ELSE 0 END)  AS metadata_rejected,
                SUM(CASE WHEN abstract IS NOT NULL
                          AND LENGTH(TRIM(abstract)) > 50          THEN 1 ELSE 0 END)  AS abstracts_retrieved,
                SUM(CASE WHEN triage_stage='abstract_missing'
                          OR phase4d_decision='MISSING_ABSTRACT'   THEN 1 ELSE 0 END)  AS abstract_missing,
                SUM(CASE WHEN phase4d_decision='ACCEPT'            THEN 1 ELSE 0 END)  AS accept,
                SUM(CASE WHEN phase4d_decision='EDGE_CASE'         THEN 1 ELSE 0 END)  AS edge_case,
                SUM(CASE WHEN phase4d_decision='REJECT'            THEN 1 ELSE 0 END)  AS reject
            FROM article_references
            """
        ).fetchone()

        try:
            pdfs_acquired = _scalar(conn, "SELECT COUNT(*) FROM papers")
        except Exception:
            pdfs_acquired = 0

        # Unobtainable: ACCEPT rows with no PDF after at least one attempt
        try:
            unobtainable = _scalar(
                conn,
                "SELECT COUNT(*) FROM article_references "
                "WHERE phase4d_decision='ACCEPT' "
                "  AND (acquired_paper_id IS NULL OR acquired_paper_id='') "
                "  AND pdf_acquisition_attempts > 0",
            )
        except Exception:
            unobtainable = 0

    identified        = int(row["identified"] or 0)
    duplicates        = int(row["duplicates"] or 0)
    metadata_rejected = int(row["metadata_rejected"] or 0)
    screened          = identified - duplicates

    return {
        "identified":         identified,
        "duplicates":         duplicates,
        "metadata_rejected":  metadata_rejected,
        "screened":           screened,
        "abstracts_retrieved": int(row["abstracts_retrieved"] or 0),
        "abstract_missing":   int(row["abstract_missing"] or 0),
        "accept":             int(row["accept"] or 0),
        "edge_case":          int(row["edge_case"] or 0),
        "reject":             int(row["reject"] or 0),
        "pdfs_acquired":      pdfs_acquired,
        "unobtainable":       unobtainable,
    }


# ── Panel F: Null Results Ledger ──────────────────────────────────────────────

def compute_panel_f(
    db_path: str = LIFECYCLE_DB,
    query_file: str = DEFAULT_QUERY_FILE,
) -> dict:
    """
    Panel F: Null Results Ledger.

    Cross-references query_results.json against article_references to find
    research gaps for which zero papers were returned by any scraper.

    Returns:
      total_null_gaps   -- count of gaps with zero results
      null_result_gaps  -- list of {gap_id, boolean_query, ai_citation_query}
      covered_gaps      -- list of gap_ids that have >= 1 result
    """
    gaps = _load_query_results(query_file)
    if not gaps:
        return {"total_null_gaps": 0, "null_result_gaps": [], "covered_gaps": []}

    conn = _conn(db_path)
    if conn is None:
        # No DB yet -- all gaps are null
        return {
            "total_null_gaps": len(gaps),
            "null_result_gaps": [
                {"gap_id": g["gap_id"],
                 "boolean_query": g.get("boolean_query", ""),
                 "ai_citation_query": g.get("ai_citation_query", "")}
                for g in gaps
            ],
            "covered_gaps": [],
        }

    with conn:
        covered_rows = conn.execute(
            "SELECT DISTINCT discovered_query FROM article_references "
            "WHERE discovered_query LIKE 'GAP-%'"
        ).fetchall()

    covered_set = {r["discovered_query"] for r in covered_rows}

    null_gaps    = []
    covered_gaps = []
    for g in gaps:
        gid = g["gap_id"]
        if gid in covered_set:
            covered_gaps.append(gid)
        else:
            null_gaps.append({
                "gap_id":            gid,
                "boolean_query":     g.get("boolean_query", ""),
                "ai_citation_query": g.get("ai_citation_query", ""),
            })

    return {
        "total_null_gaps":  len(null_gaps),
        "null_result_gaps": null_gaps,
        "covered_gaps":     covered_gaps,
    }


# ── Master aggregator ─────────────────────────────────────────────────────────

def compute_prisma_dashboard_data(
    db_path: str = LIFECYCLE_DB,
    query_file: str = DEFAULT_QUERY_FILE,
) -> dict:
    """
    Aggregate all six panels into one dashboard payload.

    This is the single entry point for both the JSON exporter and the API route.

    Returns a dict with keys: panel_a ... panel_f, generated_at, db_path.
    Every value is a plain Python dict / list (JSON-serialisable).
    """
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "db_path":      db_path,
        "panel_a":      compute_panel_a(db_path, query_file),
        "panel_b":      compute_panel_b(db_path, query_file),
        "panel_c":      compute_panel_c(db_path),
        "panel_d":      compute_panel_d(db_path),
        "panel_e":      compute_panel_e(db_path),
        "panel_f":      compute_panel_f(db_path, query_file),
    }
