"""
lifecycle_db.py — Phase 3 coordinator for pipeline_lifecycle_full.db.

This module is the single write gate for the article_references table
inside pipeline_lifecycle_full.db.  Every harvested candidate must pass
through upsert_reference() before any triage, abstract collection, or
download occurs.  Free-floating JSON files do not count.

Schema enforced by init_lifecycle_db():
  article_references  — candidate papers, one row per unique DOI (or title)
  pdf_identity_inventory — known-ingested PDFs for cross-corpus dedup

Deduplication contract (3B):
  1. Normalize DOI (strip URL prefix, lowercase).
  2. If normalized DOI already exists → UPDATE discovered_via (append),
     do not insert a duplicate row.
  3. If no DOI → check title_normalized against pdf_identity_inventory
     using Jaccard token similarity.  If similarity >= FUZZY_THRESHOLD
     → insert with triage_stage = 'duplicate'.
  4. Otherwise → INSERT new row, triage_stage = 'metadata_only'.

reference_id format: REF-YYYY-MM-DD-NNNNNN
  Date is the UTC insertion date.  Sequence is per-day, zero-padded to 6.

discovered_via enum:
  review_pdf_extract | serpapi_scholar | scholarly_search |
  paperscraper_search | openalex_expansion | crossref_search | student_upload
"""
from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── Config ─────────────────────────────────────────────────────────────────────

_HERE = Path(__file__).resolve().parent
LIFECYCLE_DB = os.environ.get(
    "LIFECYCLE_DB",
    str(_HERE.parent.parent.parent / "data" / "ka_payloads" / "pipeline_lifecycle_full.db"),
)

FUZZY_THRESHOLD = 0.85  # Jaccard similarity threshold for title-based dedup

DISCOVERED_VIA_ENUM = frozenset({
    "review_pdf_extract",
    "serpapi_scholar",
    "scholarly_search",
    "paperscraper_search",
    "openalex_expansion",
    "crossref_search",
    "student_upload",
})

# Maps harvest_layer.scraper_source → discovered_via enum value
SCRAPER_SOURCE_MAP: dict[str, str] = {
    "serpapi":               "serpapi_scholar",
    "scholarly":             "scholarly_search",
    "paper_scraper_pubmed":  "paperscraper_search",
    "paper_scraper_arxiv":   "paperscraper_search",
    "openalex":              "openalex_expansion",
    "crossref":              "crossref_search",
    "review_pdf_extract":    "review_pdf_extract",
    "student_upload":        "student_upload",
}


# ── Phase 4 triage_stage enum values ──────────────────────────────────────────
#
# State machine for triage_stage:
#
#   metadata_only          → default at INSERT
#   duplicate              → fuzzy title match at INSERT (terminal, set by upsert_reference)
#   rejected_at_metadata   → Phase 4A hard-stop (TERMINAL — no further processing)
#   abstract_pending       → Phase 4A survivor; waiting for abstract collection
#   abstract_collected     → Phase 4B success; abstract stored
#   abstract_missing       → Phase 4B terminal exhaustion; all sources returned empty
#
# Phase 4C (abstract-based domain classification) reads 'abstract_collected' rows.
# It is NOT implemented here and will be provided under a separate contract.

TRIAGE_STAGE_METADATA_ONLY          = "metadata_only"
TRIAGE_STAGE_DUPLICATE              = "duplicate"
TRIAGE_STAGE_REJECTED_METADATA      = "rejected_at_metadata"
TRIAGE_STAGE_ABSTRACT_PENDING       = "abstract_pending"
TRIAGE_STAGE_ABSTRACT_COLLECTED     = "abstract_collected"
TRIAGE_STAGE_ABSTRACT_MISSING       = "abstract_missing"
# Phase 4D terminal states
TRIAGE_STAGE_ACCEPTED_FOR_DOWNLOAD  = "accepted_for_download"
TRIAGE_STAGE_EDGE_CASE_REVIEW       = "edge_case_review"
TRIAGE_STAGE_REJECTED_AT_ABSTRACT   = "rejected_at_abstract"


# ── Schema ─────────────────────────────────────────────────────────────────────

_DDL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS article_references (
    -- Identity
    reference_id          TEXT    PRIMARY KEY,          -- REF-YYYY-MM-DD-NNNNNN
    doi                   TEXT,                         -- normalized: lowercase, no URL prefix
    title_raw             TEXT    NOT NULL DEFAULT '',
    title_normalized      TEXT    NOT NULL DEFAULT '',  -- lowercase, punctuation stripped
    first_author_surname  TEXT    NOT NULL DEFAULT '',
    publication_year      INTEGER,
    venue                 TEXT    NOT NULL DEFAULT '',

    -- Provenance
    discovered_via        TEXT    NOT NULL,             -- enum (see DISCOVERED_VIA_ENUM)
    discovered_from_paper_id TEXT,                      -- FK to another reference_id (PDF harvest)
    discovered_query      TEXT    NOT NULL DEFAULT '',
    discovery_run_id      TEXT    NOT NULL DEFAULT '',

    -- Triage state
    triage_stage          TEXT    NOT NULL DEFAULT 'metadata_only',
    discovered_at         TEXT    NOT NULL,             -- ISO 8601 UTC

    -- Raw evidence
    raw_citation          TEXT    NOT NULL DEFAULT '',
    snippet               TEXT    NOT NULL DEFAULT ''
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_lifecycle_doi
    ON article_references(doi)
    WHERE doi IS NOT NULL AND doi != '';

CREATE INDEX IF NOT EXISTS idx_lifecycle_title
    ON article_references(title_normalized);

CREATE INDEX IF NOT EXISTS idx_lifecycle_triage
    ON article_references(triage_stage);

CREATE INDEX IF NOT EXISTS idx_lifecycle_run
    ON article_references(discovery_run_id);

-- Known-ingested PDF identities — used for cross-corpus fuzzy title dedup.
-- Populated by the Article Eater / review-PDF harvest pipeline when available.
-- Starts empty; the fuzzy check gracefully returns no match on an empty table.
CREATE TABLE IF NOT EXISTS pdf_identity_inventory (
    reference_id          TEXT    PRIMARY KEY,
    doi                   TEXT,
    title_normalized      TEXT    NOT NULL DEFAULT '',
    sha256                TEXT,
    ingested_at           TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pii_title
    ON pdf_identity_inventory(title_normalized);
"""

# ── Phase 4 column migrations ──────────────────────────────────────────────────
# Applied via _apply_phase4_migrations() — safe to call on DBs created before
# Phase 4 columns were added.  Each statement is wrapped in a try/except so
# that "duplicate column" errors are silently ignored.

_PHASE4_MIGRATIONS: list[str] = [
    # Phase 4A: classifier confidence score written at metadata gate
    "ALTER TABLE article_references ADD COLUMN metadata_confidence REAL",
    # Phase 4B: abstract text and its source provider
    "ALTER TABLE article_references ADD COLUMN abstract TEXT",
    "ALTER TABLE article_references ADD COLUMN abstract_source TEXT NOT NULL DEFAULT ''",
    # Audit timestamps
    "ALTER TABLE article_references ADD COLUMN phase4a_at TEXT",
    "ALTER TABLE article_references ADD COLUMN phase4b_at TEXT",
    # Phase 4D: abstract triage decision columns
    "ALTER TABLE article_references ADD COLUMN phase4d_decision TEXT",
    "ALTER TABLE article_references ADD COLUMN phase4d_reason TEXT",
    "ALTER TABLE article_references ADD COLUMN phase4d_topic_confidence REAL",
    "ALTER TABLE article_references ADD COLUMN phase4d_voi_score REAL",
    "ALTER TABLE article_references ADD COLUMN phase4d_at TEXT",
    # Infrastructure durability: study type (from estimate_study_type())
    "ALTER TABLE article_references ADD COLUMN study_type TEXT NOT NULL DEFAULT ''",
]

# pdf_corpus_inventory DDL — canonical cross-corpus deduplication registry.
# Named to match the course spec's `pdf_corpus_inventory` table requirement.
# Functionally equivalent to pdf_identity_inventory but uses the canonical name
# so that course tooling can reference it.
_PDF_CORPUS_INVENTORY_DDL = """
CREATE TABLE IF NOT EXISTS pdf_corpus_inventory (
    reference_id      TEXT    PRIMARY KEY,
    doi               TEXT,
    title_normalized  TEXT    NOT NULL DEFAULT '',
    sha256            TEXT,
    local_path        TEXT    NOT NULL DEFAULT '',
    ingested_at       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pci_doi
    ON pdf_corpus_inventory(doi)
    WHERE doi IS NOT NULL AND doi != '';
CREATE INDEX IF NOT EXISTS idx_pci_title
    ON pdf_corpus_inventory(title_normalized);
"""

# Phase 5 column migrations
_PHASE5_MIGRATIONS: list[str] = [
    "ALTER TABLE article_references ADD COLUMN pdf_acquisition_attempts    INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE article_references ADD COLUMN pdf_acquisition_last_source TEXT    NOT NULL DEFAULT ''",
    "ALTER TABLE article_references ADD COLUMN acquired_paper_id           TEXT",
    "ALTER TABLE article_references ADD COLUMN pdf_path                    TEXT",
    "ALTER TABLE article_references ADD COLUMN phase5_status               TEXT    NOT NULL DEFAULT 'pending'",
    "ALTER TABLE article_references ADD COLUMN phase5_at                   TEXT",
]

# Phase 5C: v_acquisition_queue convenience view
# Workers read EXCLUSIVELY from this view. Never query article_references directly.
_VIEW_DDL = """
CREATE VIEW IF NOT EXISTS v_acquisition_queue AS
SELECT
    reference_id,
    doi,
    title_raw,
    first_author_surname,
    publication_year,
    venue,
    phase4d_decision,
    phase4d_voi_score,
    phase4d_topic_confidence,
    triage_stage,
    abstract,
    pdf_acquisition_attempts,
    pdf_acquisition_last_source,
    acquired_paper_id,
    phase5_status,
    phase5_at
FROM article_references
WHERE phase4d_decision = 'ACCEPT'
  AND (acquired_paper_id IS NULL OR acquired_paper_id = '')
  AND phase5_status      = 'pending';
"""

# Phase 5 acquisition attempt audit log
_PHASE5B_DDL = """
CREATE TABLE IF NOT EXISTS lifecycle_transitions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    reference_id    TEXT    NOT NULL,
    source          TEXT    NOT NULL,
    outcome         TEXT    NOT NULL,
    timestamp       TEXT    NOT NULL,
    doi             TEXT    NOT NULL DEFAULT '',
    metadata        TEXT    NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_lt_reference
    ON lifecycle_transitions(reference_id);

CREATE INDEX IF NOT EXISTS idx_lt_source
    ON lifecycle_transitions(source);
"""

# Phase 5 papers table — registered PDF files
_PHASE5_DDL = """
CREATE TABLE IF NOT EXISTS papers (
    paper_id            TEXT    PRIMARY KEY,
    reference_id        TEXT    NOT NULL,
    doi                 TEXT    NOT NULL DEFAULT '',
    local_pdf_path      TEXT    NOT NULL,
    acquisition_source  TEXT    NOT NULL,
    file_size_bytes     INTEGER,
    sha256              TEXT    NOT NULL DEFAULT '',
    acquired_at         TEXT    NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_papers_reference
    ON papers(reference_id);

CREATE INDEX IF NOT EXISTS idx_papers_doi
    ON papers(doi)
    WHERE doi != '';
"""

# Phase 4D supplementary tables (created via _apply_phase4d_tables)
_PHASE4D_DDL = """
CREATE TABLE IF NOT EXISTS edge_case_review_queue (
    reference_id        TEXT    PRIMARY KEY,
    queued_at           TEXT    NOT NULL,
    manual_review_flag  INTEGER NOT NULL DEFAULT 1,
    topic_confidence    REAL,
    voi_score           REAL,
    reason              TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS triage_decision_log (
    log_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    reference_id        TEXT    NOT NULL,
    decision            TEXT    NOT NULL,
    reason              TEXT    NOT NULL DEFAULT '',
    topic_confidence    REAL,
    voi_score           REAL,
    logged_at           TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tdl_reference
    ON triage_decision_log(reference_id);

CREATE INDEX IF NOT EXISTS idx_tdl_decision
    ON triage_decision_log(decision);
"""


def _apply_phase4_migrations(conn: sqlite3.Connection) -> None:
    """
    Idempotently add Phase 4 columns and pdf_corpus_inventory table.
    Safe to call on both brand-new and pre-existing databases.
    """
    for sql in _PHASE4_MIGRATIONS:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass  # column already exists — ignore
    conn.executescript(_PDF_CORPUS_INVENTORY_DDL)


def _apply_phase4d_tables(conn: sqlite3.Connection) -> None:
    """Create Phase 4D supplementary tables (idempotent via IF NOT EXISTS)."""
    conn.executescript(_PHASE4D_DDL)


def _apply_phase5_migrations(conn: sqlite3.Connection) -> None:
    """Idempotently add Phase 5 columns and create papers + lifecycle_transitions tables."""
    for sql in _PHASE5_MIGRATIONS:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.executescript(_PHASE5_DDL)
    conn.executescript(_PHASE5B_DDL)
    conn.executescript(_VIEW_DDL)


# ── Connection ─────────────────────────────────────────────────────────────────

def get_connection(db_path: str = LIFECYCLE_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_lifecycle_db(db_path: str = LIFECYCLE_DB) -> None:
    """Create tables, indexes, and apply Phase 4 column migrations.
    Safe to call repeatedly — all DDL uses IF NOT EXISTS / try-except."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with get_connection(db_path) as conn:
        conn.executescript(_DDL)
        _apply_phase4_migrations(conn)
        _apply_phase4d_tables(conn)
        _apply_phase5_migrations(conn)


# ── Canonical helpers (implements 3C normalize_doi contract) ───────────────────

def normalize_doi(doi: str) -> str:
    """
    Strip URL prefixes, lowercase, trim whitespace.

    This is the canonical normalize_doi() the Phase 3 spec requires.
    Implements the same contract as build_neuro_review_acquisition_queue.py
    would provide if that file were present.

    Examples:
        'https://doi.org/10.1016/J.BUILDENV.2023.110241'
        → '10.1016/j.buildenv.2023.110241'

        'DOI: 10.1093/brain/awz123'
        → '10.1093/brain/awz123'
    """
    if not doi:
        return ""
    doi = doi.strip()
    # Strip leading "DOI:" or "doi:" label
    doi = re.sub(r"^doi\s*:\s*", "", doi, flags=re.IGNORECASE)
    # Strip URL prefixes
    for prefix in (
        "https://doi.org/",
        "http://doi.org/",
        "https://dx.doi.org/",
        "http://dx.doi.org/",
    ):
        if doi.lower().startswith(prefix):
            doi = doi[len(prefix):]
            break
    return doi.strip("/").lower()


def normalize_title(title: str) -> str:
    """
    Lowercase, remove punctuation, collapse whitespace.
    Used for fuzzy title matching against pdf_identity_inventory.
    """
    if not title:
        return ""
    t = title.lower()
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _extract_first_author_surname(authors) -> str:
    """Extract the first author's surname from a list or string."""
    if not authors:
        return ""
    if isinstance(authors, list):
        first = str(authors[0]) if authors else ""
    else:
        first = str(authors).split(",")[0]
    # "Smith, J." or "J. Smith" → "Smith"
    parts = first.replace(",", " ").split()
    if not parts:
        return ""
    # Heuristic: longest token is the surname
    return max(parts, key=len).strip()


# ── reference_id generation ────────────────────────────────────────────────────

def _generate_reference_id(conn: sqlite3.Connection) -> str:
    """
    Generate the next REF-YYYY-MM-DD-NNNNNN for today (UTC).
    Sequence resets each calendar day.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prefix = f"REF-{today}-"
    count = conn.execute(
        "SELECT COUNT(*) FROM article_references WHERE reference_id LIKE ?",
        (f"{prefix}%",),
    ).fetchone()[0]
    return f"{prefix}{count + 1:06d}"


# ── Fuzzy title dedup against pdf_identity_inventory ──────────────────────────

def _fuzzy_title_match(
    conn: sqlite3.Connection,
    title_normalized: str,
    threshold: float = FUZZY_THRESHOLD,
) -> Optional[str]:
    """
    Return the reference_id of any pdf_identity_inventory row whose
    title_normalized has Jaccard token similarity >= threshold with the
    candidate title.  Returns None if no match or table is empty.
    """
    if not title_normalized:
        return None
    words_a = set(title_normalized.split())
    if not words_a:
        return None

    rows = conn.execute(
        "SELECT reference_id, title_normalized FROM pdf_identity_inventory"
    ).fetchall()

    for row in rows:
        existing = (row["title_normalized"] or "").strip()
        if not existing:
            continue
        words_b = set(existing.split())
        if not words_b:
            continue
        union = len(words_a | words_b)
        if union == 0:
            continue
        similarity = len(words_a & words_b) / union
        if similarity >= threshold:
            return row["reference_id"]

    return None


# ── Core upsert — enforces 3B deduplication ───────────────────────────────────

class UpsertResult:
    INSERTED  = "inserted"
    DOI_MERGE = "doi_merge"   # DOI already existed; discovered_via appended
    DUPLICATE = "duplicate"   # Fuzzy title match → inserted as duplicate
    SKIPPED   = "skipped"     # Validation failure (bad discovered_via etc.)


def upsert_reference(
    record: dict,
    db_path: str = LIFECYCLE_DB,
    *,
    dry_run: bool = False,
) -> tuple[str, str]:
    """
    Insert or merge a candidate into article_references.

    Returns (UpsertResult constant, reference_id).

    record keys (all optional except title_raw and discovered_via):
        title_raw, doi, authors, publication_year, venue,
        discovered_via, discovered_from_paper_id,
        discovered_query, discovery_run_id,
        raw_citation, snippet, gap_id (passed through to discovered_query)

    Raises ValueError if discovered_via is not in DISCOVERED_VIA_ENUM.
    """
    # ── Validate discovered_via ────────────────────────────────────────────────
    discovered_via = (record.get("discovered_via") or "").strip()
    if discovered_via not in DISCOVERED_VIA_ENUM:
        raise ValueError(
            f"discovered_via={discovered_via!r} is not in the allowed enum. "
            f"Allowed: {sorted(DISCOVERED_VIA_ENUM)}"
        )

    # ── Normalize fields ───────────────────────────────────────────────────────
    doi_raw        = record.get("doi") or ""
    doi_norm       = normalize_doi(doi_raw)
    title_raw      = (record.get("title_raw") or record.get("title") or "").strip()
    title_norm     = normalize_title(title_raw)
    first_author   = _extract_first_author_surname(
        record.get("authors") or record.get("first_author_surname") or ""
    )
    year           = record.get("publication_year") or record.get("year")
    venue          = (record.get("venue") or "").strip()
    disc_query     = (record.get("discovered_query") or record.get("gap_id") or "").strip()
    run_id         = (record.get("discovery_run_id") or "").strip()
    raw_citation   = (record.get("raw_citation") or "").strip()
    snippet        = (record.get("snippet") or "").strip()
    from_paper_id  = record.get("discovered_from_paper_id")
    discovered_at  = datetime.now(timezone.utc).isoformat()

    if dry_run:
        # Report what would happen without touching the DB
        return _dry_run_check(doi_norm, title_norm, discovered_via, db_path)

    with get_connection(db_path) as conn:
        # ── Rule 1: DOI match → UPDATE discovered_via, do not insert ──────────
        if doi_norm:
            existing = conn.execute(
                "SELECT reference_id, discovered_via FROM article_references WHERE doi = ?",
                (doi_norm,),
            ).fetchone()
            if existing:
                ref_id    = existing["reference_id"]
                old_via   = existing["discovered_via"] or ""
                # Append if not already present
                if discovered_via not in old_via.split(", "):
                    new_via = f"{old_via}, {discovered_via}" if old_via else discovered_via
                    conn.execute(
                        "UPDATE article_references SET discovered_via = ? WHERE reference_id = ?",
                        (new_via, ref_id),
                    )
                return UpsertResult.DOI_MERGE, ref_id

        # ── Rule 2: No DOI → fuzzy title check against pdf_identity_inventory ──
        duplicate_of = None
        triage_stage = "metadata_only"
        if not doi_norm and title_norm:
            match_id = _fuzzy_title_match(conn, title_norm)
            if match_id:
                duplicate_of = match_id
                triage_stage = "duplicate"

        # ── Rule 3: INSERT ─────────────────────────────────────────────────────
        ref_id = _generate_reference_id(conn)
        conn.execute(
            """
            INSERT INTO article_references (
                reference_id, doi, title_raw, title_normalized,
                first_author_surname, publication_year, venue,
                discovered_via, discovered_from_paper_id,
                discovered_query, discovery_run_id,
                triage_stage, discovered_at,
                raw_citation, snippet
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ref_id, doi_norm or None, title_raw, title_norm,
                first_author, year, venue,
                discovered_via, duplicate_of or from_paper_id,
                disc_query, run_id,
                triage_stage, discovered_at,
                raw_citation, snippet,
            ),
        )

    result = UpsertResult.DUPLICATE if triage_stage == "duplicate" else UpsertResult.INSERTED
    return result, ref_id


def _dry_run_check(
    doi_norm: str,
    title_norm: str,
    discovered_via: str,
    db_path: str,
) -> tuple[str, str]:
    """Report what upsert_reference would do without writing."""
    if not Path(db_path).exists() or Path(db_path).stat().st_size == 0:
        return UpsertResult.INSERTED, "(dry-run — DB not initialised)"
    with get_connection(db_path) as conn:
        if doi_norm:
            row = conn.execute(
                "SELECT reference_id FROM article_references WHERE doi = ?",
                (doi_norm,),
            ).fetchone()
            if row:
                return UpsertResult.DOI_MERGE, row["reference_id"]
        if title_norm:
            match_id = _fuzzy_title_match(conn, title_norm)
            if match_id:
                return UpsertResult.DUPLICATE, match_id
    return UpsertResult.INSERTED, "(dry-run)"


# ── Phase 4 state management ──────────────────────────────────────────────────

def update_triage_stage(
    reference_id: str,
    stage: str,
    *,
    metadata_confidence: Optional[float] = None,
    abstract: Optional[str] = None,
    abstract_source: Optional[str] = None,
    db_path: str = LIFECYCLE_DB,
) -> None:
    """
    Transition a row's triage_stage and optionally store Phase 4 results.

    Used by:
      - triage_funnel.run_phase4a_metadata_gate()  → stage ∈ {rejected_at_metadata, abstract_pending}
      - triage_funnel.run_phase4b_abstract_collection() → stage ∈ {abstract_collected, abstract_missing}

    metadata_confidence: classifier score from Phase 4A (float 0.0–1.0)
    abstract / abstract_source: filled by Phase 4B on success
    """
    now = datetime.now(timezone.utc).isoformat()
    is_4a = stage in (TRIAGE_STAGE_REJECTED_METADATA, TRIAGE_STAGE_ABSTRACT_PENDING)
    is_4b = stage in (TRIAGE_STAGE_ABSTRACT_COLLECTED, TRIAGE_STAGE_ABSTRACT_MISSING)

    with get_connection(db_path) as conn:
        conn.execute(
            """
            UPDATE article_references
               SET triage_stage        = ?,
                   metadata_confidence = COALESCE(?, metadata_confidence),
                   abstract            = COALESCE(?, abstract),
                   abstract_source     = CASE WHEN ? != '' THEN ? ELSE abstract_source END,
                   phase4a_at          = CASE WHEN ? THEN ? ELSE phase4a_at END,
                   phase4b_at          = CASE WHEN ? THEN ? ELSE phase4b_at END
             WHERE reference_id = ?
            """,
            (
                stage,
                metadata_confidence,           # replaces if provided
                abstract,                       # replaces if provided
                abstract_source or "", abstract_source or "",  # conditional replace
                is_4a, now,                    # phase4a_at only set on 4A transitions
                is_4b, now,                    # phase4b_at only set on 4B transitions
                reference_id,
            ),
        )
        # C5 ATOMIC TRANSITION LOG — every triage_stage change writes one row
        # to lifecycle_transitions so the full state machine is auditable.
        conn.execute(
            "INSERT INTO lifecycle_transitions "
            "(reference_id, source, outcome, timestamp, doi, metadata) "
            "VALUES (?, ?, ?, ?, '', ?)",
            (
                reference_id,
                "triage_stage_update",
                stage,
                now,
                f'{{"confidence": {metadata_confidence}, "abstract_source": "{abstract_source or ""}"}}',
            ),
        )


def get_pending_metadata_triage(db_path: str = LIFECYCLE_DB) -> list[dict]:
    """
    Return rows with triage_stage = 'metadata_only'.
    These are Phase 4A candidates — not yet screened through the metadata gate.
    """
    if not Path(db_path).exists() or Path(db_path).stat().st_size == 0:
        return []
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM article_references WHERE triage_stage = ?",
            (TRIAGE_STAGE_METADATA_ONLY,),
        ).fetchall()
    return [dict(r) for r in rows]


def read_acquisition_queue(
    db_path: str = LIFECYCLE_DB,
    *,
    limit: Optional[int] = None,
) -> list[dict]:
    """
    Read from v_acquisition_queue sorted by phase4d_voi_score DESC NULLS LAST.

    This is the ONLY sanctioned entry point for the Phase 5C worker loop.
    Workers must not query article_references directly.

    Returns rows ordered by VOI score so the highest-value papers are always
    processed first.  Records with NULL voi_score fall to the end.

    Parameters
    ----------
    limit : optional cap on number of rows returned (for batch sizing)
    """
    if not Path(db_path).exists() or Path(db_path).stat().st_size == 0:
        return []
    sql = (
        "SELECT * FROM v_acquisition_queue "
        "ORDER BY phase4d_voi_score DESC NULLS LAST"
    )
    params: tuple = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)
    with get_connection(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_unobtainable_candidates(
    db_path: str = LIFECYCLE_DB,
    *,
    min_attempts: int = 1,
) -> list[dict]:
    """
    Return ACCEPT rows that have been attempted but not yet acquired.

    These are the 'wanted-but-unobtainable' records for the PRISMA dashboard.
    They remain in v_acquisition_queue (phase5_status='pending') but have
    pdf_acquisition_attempts >= min_attempts and no acquired_paper_id.
    """
    if not Path(db_path).exists() or Path(db_path).stat().st_size == 0:
        return []
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT reference_id, doi, title_raw, pdf_acquisition_attempts,
                   pdf_acquisition_last_source, phase4d_voi_score
              FROM article_references
             WHERE phase4d_decision = 'ACCEPT'
               AND (acquired_paper_id IS NULL OR acquired_paper_id = '')
               AND pdf_acquisition_attempts >= ?
             ORDER BY pdf_acquisition_attempts DESC, phase4d_voi_score DESC NULLS LAST
            """,
            (min_attempts,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_accepted_for_pdf_download(db_path: str = LIFECYCLE_DB) -> list[dict]:
    """
    Return rows eligible for Phase 5 PDF acquisition.

    ELIGIBILITY CONTRACT (non-negotiable):
      - phase4d_decision = 'ACCEPT'   -- set by abstract_triage_4d.py Phase 4D
      - phase5_status    = 'pending'  -- not yet attempted or re-queued
      - acquired_paper_id IS NULL     -- no PDF registered yet

    EDGE_CASE, REJECT, and MISSING_ABSTRACT rows are NEVER returned here.
    This query is the single enforcement point for the Phase 5 gate.
    """
    if not Path(db_path).exists() or Path(db_path).stat().st_size == 0:
        return []
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM article_references
             WHERE phase4d_decision = 'ACCEPT'
               AND phase5_status    = 'pending'
               AND (acquired_paper_id IS NULL OR acquired_paper_id = '')
            """,
        ).fetchall()
    return [dict(r) for r in rows]


def increment_pdf_attempt(
    reference_id: str,
    source_name: str,
    db_path: str = LIFECYCLE_DB,
) -> None:
    """
    Increment pdf_acquisition_attempts by 1 and record the source just tried.
    Called ONCE per source attempt, regardless of outcome.

    This is the atomic telemetry write that Gemini's Stage 3 was missing.
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_connection(db_path) as conn:
        conn.execute(
            """
            UPDATE article_references
               SET pdf_acquisition_attempts    = pdf_acquisition_attempts + 1,
                   pdf_acquisition_last_source = ?,
                   phase5_at                  = COALESCE(phase5_at, ?)
             WHERE reference_id = ?
            """,
            (source_name, now, reference_id),
        )


def mark_pdf_acquired(
    reference_id: str,
    *,
    paper_id: str,
    pdf_path: str,
    db_path: str = LIFECYCLE_DB,
) -> None:
    """
    Record a successful PDF acquisition on article_references.
    Maps acquired_paper_id -> papers.paper_id.
    """
    with get_connection(db_path) as conn:
        conn.execute(
            """
            UPDATE article_references
               SET phase5_status    = 'pdf_acquired',
                   acquired_paper_id= ?,
                   pdf_path         = ?
             WHERE reference_id = ?
            """,
            (paper_id, pdf_path, reference_id),
        )


def log_transition(
    reference_id: str,
    source: str,
    outcome: str,
    *,
    doi: str = "",
    metadata: Optional[str] = None,
    db_path: str = LIFECYCLE_DB,
) -> None:
    """
    Append one row to lifecycle_transitions.

    Called by the acquisition engine after EVERY source attempt (success or failure)
    and immediately before/after any scidownl invocation.

    outcome: 'success' | 'failure' | 'skipped' | 'gate_blocked'
    metadata: optional JSON string with extra context (e.g. HTTP status, file size)
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO lifecycle_transitions
                (reference_id, source, outcome, timestamp, doi, metadata)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (reference_id, source, outcome, now, doi or "", metadata or "{}"),
        )


def get_acquisition_attempts_for_record(
    reference_id: str,
    db_path: str = LIFECYCLE_DB,
) -> list[dict]:
    """
    Return all lifecycle_transitions rows for a given reference_id.
    Used by Phase 5B gate to verify cascade exhaustion.
    """
    if not Path(db_path).exists() or Path(db_path).stat().st_size == 0:
        return []
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM lifecycle_transitions WHERE reference_id = ? ORDER BY id",
            (reference_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_pdf_not_found(reference_id: str, db_path: str = LIFECYCLE_DB) -> None:
    """Record terminal PDF acquisition failure (all sources exhausted)."""
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE article_references SET phase5_status = 'pdf_not_found' WHERE reference_id = ?",
            (reference_id,),
        )


def register_paper(
    *,
    paper_id: str,
    reference_id: str,
    doi: str,
    local_pdf_path: str,
    acquisition_source: str,
    file_size_bytes: int,
    sha256: str,
    db_path: str = LIFECYCLE_DB,
) -> None:
    """
    Insert a new row into the papers table.
    Called exactly once per successful PDF download.
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO papers
                (paper_id, reference_id, doi, local_pdf_path,
                 acquisition_source, file_size_bytes, sha256, acquired_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                paper_id, reference_id, doi, local_pdf_path,
                acquisition_source, file_size_bytes, sha256, now,
            ),
        )


def _generate_paper_id(conn: sqlite3.Connection) -> str:
    """Generate a stable PDF-YYYY-MM-DD-NNNNNN identifier."""
    today  = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prefix = f"PDF-{today}-"
    count  = conn.execute(
        "SELECT COUNT(*) FROM papers WHERE paper_id LIKE ?",
        (f"{prefix}%",),
    ).fetchone()[0]
    return f"{prefix}{count + 1:06d}"


def get_abstract_collected_rows(db_path: str = LIFECYCLE_DB) -> list[dict]:
    """
    Return rows with triage_stage = 'abstract_collected'.
    These are Phase 4D candidates -- abstract collected, awaiting final triage.
    """
    if not Path(db_path).exists() or Path(db_path).stat().st_size == 0:
        return []
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM article_references WHERE triage_stage = ?",
            (TRIAGE_STAGE_ABSTRACT_COLLECTED,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_pending_abstract_collection(db_path: str = LIFECYCLE_DB) -> list[dict]:
    """
    Return rows with triage_stage = 'abstract_pending'.
    These are Phase 4B candidates — survived the metadata gate, abstract not yet fetched.
    """
    if not Path(db_path).exists() or Path(db_path).stat().st_size == 0:
        return []
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM article_references WHERE triage_stage = ?",
            (TRIAGE_STAGE_ABSTRACT_PENDING,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_phase4_counts(db_path: str = LIFECYCLE_DB) -> dict[str, int]:
    """Return per-triage_stage row counts for Phase 4 reporting."""
    if not Path(db_path).exists() or Path(db_path).stat().st_size == 0:
        return {s: 0 for s in (
            TRIAGE_STAGE_METADATA_ONLY, TRIAGE_STAGE_REJECTED_METADATA,
            TRIAGE_STAGE_ABSTRACT_PENDING, TRIAGE_STAGE_ABSTRACT_COLLECTED,
            TRIAGE_STAGE_ABSTRACT_MISSING, TRIAGE_STAGE_DUPLICATE,
        )}
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT triage_stage, COUNT(*) AS n FROM article_references GROUP BY triage_stage"
        ).fetchall()
    return {row["triage_stage"]: row["n"] for row in rows}


# ── Batch harvest bridge ───────────────────────────────────────────────────────

def write_candidates_to_lifecycle(
    candidates: list[dict],
    *,
    db_path: str = LIFECYCLE_DB,
    discovery_run_id: str = "",
    dry_run: bool = False,
    triage_gate: Optional[object] = None,
) -> dict[str, int]:
    """
    Write a list of harvest_layer candidate dicts into article_references.

    Maps harvest_layer field names → lifecycle schema field names.
    Returns counts: {inserted, doi_merge, duplicate, skipped, error}.

    triage_gate (optional):
        If provided, must be a callable with signature:
            triage_gate(row: dict, db_path: str) -> None
        Called immediately after each freshly INSERTED row (not for doi_merge or
        duplicate rows).  This is the Phase 4A inline hook — it runs the metadata
        gate at ingest, inside the insertion loop, before any abstract fetch.

        Pass triage_funnel.classify_at_ingest to activate Phase 4A at ingest time:
            from triage_funnel import classify_at_ingest
            write_candidates_to_lifecycle(candidates, triage_gate=classify_at_ingest)
    """
    import sys as _sys

    counts = {
        UpsertResult.INSERTED:  0,
        UpsertResult.DOI_MERGE: 0,
        UpsertResult.DUPLICATE: 0,
        UpsertResult.SKIPPED:   0,
        "error":                0,
    }

    if not dry_run:
        init_lifecycle_db(db_path)

    for cand in candidates:
        # Map scraper_source → discovered_via enum
        src = cand.get("scraper_source") or cand.get("discovered_via") or ""
        discovered_via = SCRAPER_SOURCE_MAP.get(src, src)
        if discovered_via not in DISCOVERED_VIA_ENUM:
            counts[UpsertResult.SKIPPED] += 1
            continue

        record = {
            "title_raw":             cand.get("title", ""),
            "doi":                   cand.get("doi", ""),
            "authors":               cand.get("authors", []),
            "publication_year":      cand.get("year"),
            "venue":                 cand.get("venue", ""),
            "discovered_via":        discovered_via,
            "discovered_query":      cand.get("gap_id") or cand.get("discovered_query", ""),
            "discovery_run_id":      discovery_run_id or cand.get("discovery_run_id", ""),
            "discovered_from_paper_id": cand.get("discovered_from_paper_id"),
            "raw_citation":          cand.get("raw_citation", ""),
            "snippet":               cand.get("snippet", ""),
        }

        try:
            result, ref_id = upsert_reference(record, db_path=db_path, dry_run=dry_run)
            counts[result] += 1

            # ── Phase 4A inline hook — fires immediately on fresh inserts ────
            # Hard-contract: triage_gate is ONLY invoked for genuinely new rows.
            # DOI merges and duplicates already have a triage_stage and must not
            # be re-processed by the metadata gate.
            if (
                result == UpsertResult.INSERTED
                and triage_gate is not None
                and not dry_run
            ):
                try:
                    # Fetch the just-inserted row so the gate has all fields
                    row_data = {"reference_id": ref_id, **record}
                    triage_gate(row_data, db_path)
                except Exception as gate_exc:
                    print(
                        f"  [lifecycle] triage_gate error on {ref_id}: {gate_exc}",
                        file=_sys.stderr,
                    )

        except Exception as exc:
            counts["error"] += 1
            print(f"  [lifecycle] Error on {record.get('title_raw','')[:60]}: {exc}", file=_sys.stderr)

    return counts


# ── PRISMA-style counts from lifecycle DB ─────────────────────────────────────

def get_lifecycle_counts(db_path: str = LIFECYCLE_DB) -> dict:
    """Return triage-stage counts from article_references."""
    if not Path(db_path).exists() or Path(db_path).stat().st_size == 0:
        return {"total": 0, "metadata_only": 0, "duplicate": 0}

    with get_connection(db_path) as conn:
        def _count(sql, params=()):
            return conn.execute(sql, params).fetchone()[0]

        total        = _count("SELECT COUNT(*) FROM article_references")
        meta_only    = _count("SELECT COUNT(*) FROM article_references WHERE triage_stage='metadata_only'")
        duplicates   = _count("SELECT COUNT(*) FROM article_references WHERE triage_stage='duplicate'")
        doi_present  = _count("SELECT COUNT(*) FROM article_references WHERE doi IS NOT NULL AND doi != ''")

    return {
        "total":        total,
        "metadata_only": meta_only,
        "duplicate":    duplicates,
        "with_doi":     doi_present,
    }


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse, json, sys

    parser = argparse.ArgumentParser(description="lifecycle_db utilities")
    parser.add_argument("--db", default=LIFECYCLE_DB)
    parser.add_argument("--init", action="store_true", help="Initialise schema")
    parser.add_argument("--counts", action="store_true", help="Print lifecycle counts")
    parser.add_argument("--dump", action="store_true", help="Dump all rows")
    args = parser.parse_args()

    if args.init:
        init_lifecycle_db(args.db)
        print(f"Lifecycle DB initialised: {args.db}")
    if args.counts:
        print(json.dumps(get_lifecycle_counts(args.db), indent=2))
    if args.dump:
        with get_connection(args.db) as conn:
            rows = conn.execute(
                "SELECT reference_id, doi, title_raw, discovered_via, triage_stage "
                "FROM article_references ORDER BY reference_id"
            ).fetchall()
        for r in rows:
            print(dict(r))
