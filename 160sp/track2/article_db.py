"""
article_db.py — SQLite article_references table schema and helpers.

Every scraper writes raw candidates here via upsert_candidate().
Triage stages update rows in-place via update_stage1/2/3().
The PRISMA funnel is always reconstructed from live row counts via
get_prisma_counts() — never from hardcoded values.

Schema
------
article_references
    id                  INTEGER PK AUTOINCREMENT
    paper_id            TEXT    UNIQUE  (scraper-source + content hash)
    gap_id              TEXT            (GAP-PNU-NNN)
    scraper_source      TEXT            (serpapi | scholarly | paper_scraper_pubmed | ...)
    title               TEXT
    doi                 TEXT
    url                 TEXT
    authors             TEXT            (JSON array of strings)
    year                INTEGER
    venue               TEXT
    cited_by            INTEGER
    snippet             TEXT            (SerpAPI snippet / bib abstract at harvest time)
    abstract            TEXT            (filled by Stage 2 abstract enrichment)
    abstract_source     TEXT            (semantic_scholar | crossref | pubmed | openalex | snippet)
    pdf_path            TEXT            (filled by Stage 3 PDF acquisition)
    stage1_status       TEXT  DEFAULT 'pending'  (pending | pass | fail)
    stage2_status       TEXT  DEFAULT 'pending'  (pending | ACCEPT | EDGE_CASE | REJECT |
                                                   MISSING_ABSTRACT | DUPLICATE)
    stage3_status       TEXT  DEFAULT 'pending'  (pending | pdf_found | pdf_not_found | skipped)
    final_decision      TEXT            (set after each stage that terminates a record)
    created_at          TEXT
    updated_at          TEXT
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DEFAULT_DB = "article_references.db"


# ── Connection ─────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_connection(db_path: str = DEFAULT_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ── Schema init ───────────────────────────────────────────────────────────────

def init_db(db_path: str = DEFAULT_DB) -> None:
    """Create tables and indexes if they do not already exist."""
    with get_connection(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS article_references (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                paper_id        TEXT    NOT NULL UNIQUE,
                gap_id          TEXT,
                scraper_source  TEXT,
                title           TEXT,
                doi             TEXT,
                url             TEXT,
                authors         TEXT,
                year            INTEGER,
                venue           TEXT,
                cited_by        INTEGER,
                snippet         TEXT,
                abstract        TEXT,
                abstract_source TEXT,
                pdf_path        TEXT,
                stage1_status   TEXT DEFAULT 'pending',
                stage2_status   TEXT DEFAULT 'pending',
                stage3_status   TEXT DEFAULT 'pending',
                final_decision  TEXT,
                created_at      TEXT,
                updated_at      TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_doi      ON article_references(doi);
            CREATE INDEX IF NOT EXISTS idx_gap      ON article_references(gap_id);
            CREATE INDEX IF NOT EXISTS idx_decision ON article_references(final_decision);
            CREATE INDEX IF NOT EXISTS idx_s1       ON article_references(stage1_status);
            CREATE INDEX IF NOT EXISTS idx_s2       ON article_references(stage2_status);
            CREATE INDEX IF NOT EXISTS idx_s3       ON article_references(stage3_status);
            """
        )


# ── Write helpers ─────────────────────────────────────────────────────────────

def upsert_candidate(record: dict, db_path: str = DEFAULT_DB) -> bool:
    """
    Insert a new candidate row.

    Returns True if the row was inserted, False if paper_id already existed
    (safe deduplication — the existing row is left unchanged).
    """
    now = _now()
    with get_connection(db_path) as conn:
        try:
            conn.execute(
                """
                INSERT INTO article_references
                    (paper_id, gap_id, scraper_source, title, doi, url,
                     authors, year, venue, cited_by, snippet,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["paper_id"],
                    record.get("gap_id"),
                    record.get("scraper_source"),
                    record.get("title", ""),
                    record.get("doi", ""),
                    record.get("url", ""),
                    json.dumps(record.get("authors") or []),
                    record.get("year"),
                    record.get("venue", ""),
                    record.get("cited_by"),
                    record.get("snippet", ""),
                    now,
                    now,
                ),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def update_stage1(paper_id: str, status: str, db_path: str = DEFAULT_DB) -> None:
    """Record Stage 1 outcome ('pass' or 'fail') for a candidate."""
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE article_references SET stage1_status=?, updated_at=? WHERE paper_id=?",
            (status, _now(), paper_id),
        )


def update_stage2(
    paper_id: str,
    *,
    status: str,
    abstract: str = "",
    abstract_source: str = "",
    doi: str = "",
    db_path: str = DEFAULT_DB,
) -> None:
    """
    Record Stage 2 outcome and persist enriched abstract.

    doi is written only if it is non-empty (the existing DOI is kept otherwise).
    """
    with get_connection(db_path) as conn:
        conn.execute(
            """
            UPDATE article_references
               SET stage2_status   = ?,
                   abstract        = ?,
                   abstract_source = ?,
                   doi             = COALESCE(NULLIF(?, ''), doi),
                   updated_at      = ?
             WHERE paper_id = ?
            """,
            (status, abstract, abstract_source, doi, _now(), paper_id),
        )


def update_stage3(
    paper_id: str,
    *,
    status: str,
    pdf_path: str = "",
    db_path: str = DEFAULT_DB,
) -> None:
    """Record Stage 3 PDF acquisition outcome."""
    with get_connection(db_path) as conn:
        conn.execute(
            """
            UPDATE article_references
               SET stage3_status = ?,
                   pdf_path      = ?,
                   updated_at    = ?
             WHERE paper_id = ?
            """,
            (status, pdf_path, _now(), paper_id),
        )


def set_final_decision(paper_id: str, decision: str, db_path: str = DEFAULT_DB) -> None:
    """Stamp the final_decision column (called by whichever stage terminates the record)."""
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE article_references SET final_decision=?, updated_at=? WHERE paper_id=?",
            (decision, _now(), paper_id),
        )


# ── Read helpers ──────────────────────────────────────────────────────────────

def get_pending_stage1(db_path: str = DEFAULT_DB) -> list[dict]:
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM article_references WHERE stage1_status = 'pending'"
        ).fetchall()
    return [dict(r) for r in rows]


def get_pending_stage2(db_path: str = DEFAULT_DB) -> list[dict]:
    """Records that passed Stage 1 and are waiting for Stage 2."""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM article_references
             WHERE stage1_status = 'pass'
               AND stage2_status = 'pending'
            """
        ).fetchall()
    return [dict(r) for r in rows]


def get_pending_stage3(db_path: str = DEFAULT_DB) -> list[dict]:
    """
    Records cleared by Stage 2 (ACCEPT or EDGE_CASE) waiting for PDF acquisition.
    PDFs are NEVER fetched before this query returns a row.
    """
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM article_references
             WHERE stage2_status IN ('ACCEPT', 'EDGE_CASE')
               AND stage3_status = 'pending'
            """
        ).fetchall()
    return [dict(r) for r in rows]


def get_all_records(db_path: str = DEFAULT_DB) -> list[dict]:
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM article_references ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


# ── PRISMA counts — live from DB, never hardcoded ────────────────────────────

def get_prisma_counts(db_path: str = DEFAULT_DB) -> dict:
    """
    Return PRISMA funnel counts computed directly from article_references.

    This is the single authoritative source for the PRISMA dashboard.
    The HTML template must call this (or read prisma_counts.json which is
    generated by exporting this function) rather than embedding numbers.
    """
    if not Path(db_path).exists():
        return {
            "identified": 0, "duplicates_removed": 0, "screened": 0,
            "with_abstract": 0, "missing_abstract": 0,
            "accept": 0, "edge_case": 0, "reject": 0, "pdfs_retrieved": 0,
        }

    with get_connection(db_path) as conn:

        def _count(sql: str, params: tuple = ()) -> int:
            return conn.execute(sql, params).fetchone()[0]

        identified       = _count("SELECT COUNT(*) FROM article_references")
        duplicates       = _count(
            "SELECT COUNT(*) FROM article_references WHERE final_decision = 'DUPLICATE'"
        )
        screened         = identified - duplicates
        with_abstract    = _count(
            """SELECT COUNT(*) FROM article_references
               WHERE abstract IS NOT NULL
                 AND TRIM(abstract) != ''
                 AND LENGTH(TRIM(abstract)) >= 20"""
        )
        missing_abstract = _count(
            "SELECT COUNT(*) FROM article_references WHERE final_decision = 'MISSING_ABSTRACT'"
        )
        accept           = _count(
            "SELECT COUNT(*) FROM article_references WHERE final_decision = 'ACCEPT'"
        )
        edge_case        = _count(
            "SELECT COUNT(*) FROM article_references WHERE final_decision = 'EDGE_CASE'"
        )
        reject           = _count(
            "SELECT COUNT(*) FROM article_references WHERE final_decision = 'REJECT'"
        )
        pdfs_retrieved   = _count(
            "SELECT COUNT(*) FROM article_references WHERE stage3_status = 'pdf_found'"
        )

    return {
        "identified":       identified,
        "duplicates_removed": duplicates,
        "screened":         screened,
        "with_abstract":    with_abstract,
        "missing_abstract": missing_abstract,
        "accept":           accept,
        "edge_case":        edge_case,
        "reject":           reject,
        "pdfs_retrieved":   pdfs_retrieved,
    }


# ── CLI convenience ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse, sys

    parser = argparse.ArgumentParser(description="article_references DB utilities")
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--init", action="store_true", help="Initialise DB schema")
    parser.add_argument("--counts", action="store_true", help="Print PRISMA counts")
    parser.add_argument("--dump", action="store_true", help="Dump all rows as JSON")
    args = parser.parse_args()

    if args.init:
        init_db(args.db)
        print(f"DB initialised: {args.db}")
    if args.counts:
        import json as _json
        print(_json.dumps(get_prisma_counts(args.db), indent=2))
    if args.dump:
        import json as _json
        print(_json.dumps(get_all_records(args.db), indent=2, ensure_ascii=False))
