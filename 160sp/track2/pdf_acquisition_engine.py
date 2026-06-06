"""
pdf_acquisition_engine.py -- Phase 5: PDF Acquisition Cascade.

Reads rows where phase4d_decision = 'ACCEPT' from pipeline_lifecycle_full.db
and executes a strict three-step acquisition cascade.  Stops immediately on
the first successful PDF download.

CASCADE ORDER (non-negotiable):
  Step 1: Unpaywall         -- legal open-access; always tried first
  Step 2: OpenAlex OA URL   -- inspects open_access.oa_url from OpenAlex API
  Step 3: scidownl          -- Sci-Hub mirror; last resort, policy-gated

PHASE 5B POLICY GATE
  Step 3 (scidownl) is blocked behind a placeholder exception until Phase 5B
  is implemented.  To call scidownl in development, pass allow_scidownl=True
  explicitly.  The gate location is clearly marked in the source.

TELEMETRY CONTRACT
  pdf_acquisition_attempts   -- incremented by 1 for EVERY source tried,
                                 regardless of outcome
  pdf_acquisition_last_source -- set to the source name after EVERY attempt
  Both are written atomically via lifecycle_db.increment_pdf_attempt().

SUCCESS CONTRACT
  On success:
    1. PDF saved to pdf_dir / <safe_doi>.pdf
    2. SHA-256 computed and stored
    3. New row inserted into the papers table via lifecycle_db.register_paper()
    4. article_references.acquired_paper_id mapped to the new papers.paper_id
    5. article_references.phase5_status = 'pdf_acquired'

FAILURE CONTRACT
  After all applicable sources are exhausted:
    phase5_status = 'pdf_not_found'
  Records are NEVER silently dropped.  Every attempted row gets a terminal status.

ELIGIBILITY CONTRACT
  ONLY rows with phase4d_decision = 'ACCEPT' are processed.
  EDGE_CASE, REJECT, and MISSING_ABSTRACT rows are excluded by the query.

Usage
-----
    python pdf_acquisition_engine.py
    python pdf_acquisition_engine.py --db /path/to/pipeline_lifecycle_full.db
    python pdf_acquisition_engine.py --pdf-dir /data/pdfs
    python pdf_acquisition_engine.py --dry-run
    python pdf_acquisition_engine.py --limit 20
    python pdf_acquisition_engine.py --allow-scidownl   # Phase 5B dev only
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path
from typing import Callable, Optional

# ── Lifecycle DB ──────────────────────────────────────────────────────────────
from lifecycle_db import (
    LIFECYCLE_DB,
    _generate_paper_id,
    get_accepted_for_pdf_download,
    get_connection,
    increment_pdf_attempt,
    init_lifecycle_db,
    log_transition,
    mark_pdf_acquired,
    mark_pdf_not_found,
    register_paper,
)

# ── PDF downloader clients (Step 1 & 2) ───────────────────────────────────────
_THIS_DIR = Path(__file__).resolve().parent
_AE_SRC   = _THIS_DIR.parent.parent.parent.parent / "Article_Eater" / "src"
if str(_AE_SRC) not in sys.path and _AE_SRC.exists():
    sys.path.insert(0, str(_AE_SRC))

try:
    from ingest.pdf_downloader import (  # type: ignore
        OpenAlexOADownloader,
        UnpaywallDownloader,
    )
    _DOWNLOADERS_AVAILABLE = True
except ImportError as _e:
    _DOWNLOADERS_AVAILABLE = False
    _DOWNLOADERS_IMPORT_ERR = str(_e)

# ── scidownl bridge (Step 3) ──────────────────────────────────────────────────
try:
    from harvest_layer import acquire_pdf_scidownl  # type: ignore
    _SCIDOWNL_AVAILABLE = True
except ImportError:
    _SCIDOWNL_AVAILABLE = False

# ── Phase 5B gate (replaces placeholder) ─────────────────────────────────────
from scidownl_policy_gate import (  # type: ignore
    ScidownlClearanceError,
    ConfigNotArmedError,
    PolicyClearanceMissingError,
    CascadeNotExhaustedError,
    TriageElevationError,
    verify_scidownl_clearance,
)

# ── Project root (Article_Eater/) — canonical location for config + clearance ─
_PROJECT_ROOT      = Path(__file__).resolve().parent.parent.parent.parent / "Article_Eater"
DEFAULT_CONFIG_PATH    = str(_PROJECT_ROOT / "acquisition_config.yaml")
DEFAULT_CLEARANCE_PATH = str(_PROJECT_ROOT / "policy_clearance.json")


# ── Backward-compat shim for Phase 5 tests ───────────────────────────────────
# The full four-condition gate lives in scidownl_policy_gate.verify_scidownl_clearance().
# This simple shim restores the original allow_scidownl boolean interface so that
# test_phase5_pdf_acquisition.py (written before Phase 5B) continues to pass.

def _phase5b_policy_gate(doi: str, *, allow_scidownl: bool) -> None:
    """Simple boolean gate — backward compat only. Real gate: verify_scidownl_clearance()."""
    if not allow_scidownl:
        raise ScidownlClearanceError(
            doi,
            f"scidownl blocked for DOI {doi!r}: allow_scidownl=False (Phase 5B gate not armed).",
        )
    print(
        f"  [phase5b] WARNING: policy gate bypassed for {doi} (allow_scidownl=True).",
        file=sys.stderr,
    )


# ── Constants ─────────────────────────────────────────────────────────────────

DEFAULT_PDF_DIR  = "pdfs"
PHASE5_STATUS_ACQUIRED   = "pdf_acquired"
PHASE5_STATUS_NOT_FOUND  = "pdf_not_found"
PHASE5_STATUS_PENDING    = "pending"

SOURCE_UNPAYWALL  = "unpaywall"
SOURCE_OPENALEX   = "openalex_oa"
SOURCE_SCIDOWNL   = "scidownl"


# Phase5BPolicyGateError kept as alias for backward compatibility with Phase 5 tests
Phase5BPolicyGateError = ScidownlClearanceError


# ── SHA-256 helper ────────────────────────────────────────────────────────────

def _sha256_file(path: str) -> str:
    """Compute SHA-256 hex digest of a local file."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


# ── Safe DOI → filename ───────────────────────────────────────────────────────

def _doi_to_filename(doi: str) -> str:
    return doi.replace("/", "_").replace(":", "_").replace(" ", "_")


# ── Single-record cascade ─────────────────────────────────────────────────────

def acquire_pdf_for_record(
    row: dict,
    *,
    pdf_dir: str = DEFAULT_PDF_DIR,
    db_path: str = LIFECYCLE_DB,
    allow_scidownl: bool = False,
    config_path: str = DEFAULT_CONFIG_PATH,
    policy_clearance_path: str = DEFAULT_CLEARANCE_PATH,
    unpaywall_client: Optional[object] = None,
    openalex_client: Optional[object] = None,
    scidownl_fn: Optional[Callable] = None,
    dry_run: bool = False,
) -> str:
    """
    Execute the three-step acquisition cascade for a single row.

    Parameters
    ----------
    row             : dict from article_references (must have 'reference_id', 'doi')
    pdf_dir         : directory to write downloaded PDFs
    db_path         : pipeline_lifecycle_full.db path
    allow_scidownl  : bypass Phase 5B gate (development only)
    unpaywall_client: injectable UnpaywallDownloader for testing
    openalex_client : injectable OpenAlexOADownloader for testing
    scidownl_fn     : injectable scidownl callable for testing
    dry_run         : if True, simulate without writing to DB or disk

    Returns
    -------
    One of: PHASE5_STATUS_ACQUIRED ('pdf_acquired') or
            PHASE5_STATUS_NOT_FOUND ('pdf_not_found')
    """
    reference_id = row.get("reference_id", "")
    doi          = (row.get("doi") or "").strip()
    title        = (row.get("title_raw") or row.get("title") or "").strip()

    # Papers without a DOI cannot use Unpaywall or scidownl; OpenAlex may still work
    if not doi:
        if not dry_run:
            mark_pdf_not_found(reference_id, db_path=db_path)
        return PHASE5_STATUS_NOT_FOUND

    Path(pdf_dir).mkdir(parents=True, exist_ok=True)
    out_path = str(Path(pdf_dir) / f"{_doi_to_filename(doi)}.pdf")
    pdf_path: Optional[str] = None

    phase4d_decision = row.get("phase4d_decision", "")

    # ── Step 1: Unpaywall ──────────────────────────────────────────────────────
    _up = unpaywall_client or (UnpaywallDownloader() if _DOWNLOADERS_AVAILABLE else None)
    if _up is not None:
        if not dry_run:
            increment_pdf_attempt(reference_id, SOURCE_UNPAYWALL, db_path=db_path)
        result = _up.try_download(doi, out_path)
        outcome_up = "success" if result else "failure"
        if not dry_run:
            log_transition(reference_id, SOURCE_UNPAYWALL, outcome_up, doi=doi, db_path=db_path)
        if result:
            pdf_path = result

    # ── Step 2: OpenAlex OA URL ───────────────────────────────────────────────
    if not pdf_path:
        _oa = openalex_client or (OpenAlexOADownloader() if _DOWNLOADERS_AVAILABLE else None)
        if _oa is not None:
            if not dry_run:
                increment_pdf_attempt(reference_id, SOURCE_OPENALEX, db_path=db_path)
            result = _oa.try_download(doi, out_path)
            outcome_oa = "success" if result else "failure"
            if not dry_run:
                log_transition(reference_id, SOURCE_OPENALEX, outcome_oa, doi=doi, db_path=db_path)
            if result:
                pdf_path = result

    # ── Step 3: scidownl — PHASE 5B FOUR-CONDITION POLICY GATE ───────────────
    #
    # verify_scidownl_clearance() enforces ALL FOUR conditions atomically:
    #   C1. acquisition_config.yaml: enable_paid_or_grey_sources == true
    #   C2. policy_clearance.json physically exists at project root
    #   C3. Both unpaywall and openalex_oa logged as 'failure' in lifecycle_transitions
    #   C4. phase4d_decision == 'ACCEPT' (defense-in-depth; already filtered upstream)
    #
    # Any single failure raises a typed ScidownlClearanceError subclass.
    # The attempt counter is NOT incremented when the gate blocks.
    if not pdf_path:
        _sci_fn = scidownl_fn or (acquire_pdf_scidownl if _SCIDOWNL_AVAILABLE else None)
        if _sci_fn is not None:
            try:
                if allow_scidownl:
                    # Simple boolean bypass (development / test use only).
                    # Still calls the compat shim so Phase 5 tests work.
                    _phase5b_policy_gate(doi, allow_scidownl=True)
                else:
                    # Production path: enforce all four policy conditions.
                    verify_scidownl_clearance(
                        reference_id,
                        phase4d_decision,
                        db_path               = db_path,
                        config_path           = config_path,
                        policy_clearance_path = policy_clearance_path,
                    )
                # All four conditions cleared -- execute scidownl
                if not dry_run:
                    increment_pdf_attempt(reference_id, SOURCE_SCIDOWNL, db_path=db_path)
                result = _sci_fn(doi, output_dir=pdf_dir)
                outcome_sci = "success" if result else "failure"
                if not dry_run:
                    log_transition(
                        reference_id, SOURCE_SCIDOWNL, outcome_sci, doi=doi, db_path=db_path
                    )
                if result:
                    pdf_path = result

            except ScidownlClearanceError as gate_exc:
                # Gate blocked — log the block, do not increment attempt counter
                if not dry_run:
                    log_transition(
                        reference_id, SOURCE_SCIDOWNL, "gate_blocked",
                        doi=doi,
                        metadata=f'{{"condition": {gate_exc.condition}, "reason": {str(gate_exc)!r}}}',
                        db_path=db_path,
                    )

    # ── Persist outcome ───────────────────────────────────────────────────────
    if dry_run:
        return PHASE5_STATUS_ACQUIRED if pdf_path else PHASE5_STATUS_NOT_FOUND

    if pdf_path:
        file_size = Path(pdf_path).stat().st_size if Path(pdf_path).exists() else 0
        checksum  = _sha256_file(pdf_path)

        with get_connection(db_path) as conn:
            paper_id = _generate_paper_id(conn)

        register_paper(
            paper_id           = paper_id,
            reference_id       = reference_id,
            doi                = doi,
            local_pdf_path     = pdf_path,
            acquisition_source = row.get("pdf_acquisition_last_source", ""),
            file_size_bytes    = file_size,
            sha256             = checksum,
            db_path            = db_path,
        )
        mark_pdf_acquired(
            reference_id,
            paper_id = paper_id,
            pdf_path = pdf_path,
            db_path  = db_path,
        )
        return PHASE5_STATUS_ACQUIRED

    mark_pdf_not_found(reference_id, db_path=db_path)
    return PHASE5_STATUS_NOT_FOUND


# ── Batch processor ───────────────────────────────────────────────────────────

def run_phase5_acquisition(
    db_path: str = LIFECYCLE_DB,
    *,
    pdf_dir: str = DEFAULT_PDF_DIR,
    allow_scidownl: bool = False,
    config_path: str = DEFAULT_CONFIG_PATH,
    policy_clearance_path: str = DEFAULT_CLEARANCE_PATH,
    dry_run: bool = False,
    limit: Optional[int] = None,
    unpaywall_client: Optional[object] = None,
    openalex_client: Optional[object] = None,
    scidownl_fn: Optional[Callable] = None,
) -> dict[str, int]:
    """
    Process all eligible ACCEPT rows through the PDF acquisition cascade.

    Eligibility: phase4d_decision = 'ACCEPT' AND phase5_status = 'pending'
    EDGE_CASE, REJECT, and MISSING_ABSTRACT rows are never touched here.

    Parameters
    ----------
    db_path         : pipeline_lifecycle_full.db
    pdf_dir         : local directory for downloaded PDFs
    allow_scidownl  : bypass Phase 5B gate (development only)
    dry_run         : simulate decisions without writing to DB or disk
    limit           : cap number of rows processed
    unpaywall_client: injectable for testing
    openalex_client : injectable for testing
    scidownl_fn     : injectable for testing

    Returns
    -------
    {
        'pdf_acquired':  N,
        'pdf_not_found': N,
        'total':         N,
    }
    """
    if not dry_run:
        init_lifecycle_db(db_path)

    if not _DOWNLOADERS_AVAILABLE and not (unpaywall_client or openalex_client):
        print(
            f"[phase5] ingest.pdf_downloader not importable: {_DOWNLOADERS_IMPORT_ERR}\n"
            "         Ensure Article_Eater/src is on PYTHONPATH.",
            file=sys.stderr,
        )

    rows = get_accepted_for_pdf_download(db_path)
    if limit is not None:
        rows = rows[:limit]

    counts = {PHASE5_STATUS_ACQUIRED: 0, PHASE5_STATUS_NOT_FOUND: 0, "total": 0}
    total  = len(rows)

    print(f"[phase5] {total} ACCEPT rows eligible for PDF acquisition")

    for i, row in enumerate(rows, 1):
        ref_id = row.get("reference_id", f"<row-{i}>")
        doi    = (row.get("doi") or "").strip()
        label  = (row.get("title_raw") or doi or ref_id)[:68]

        print(f"  [5 {i:4d}/{total}] {label}", end="\r", flush=True)

        try:
            status = acquire_pdf_for_record(
                row,
                pdf_dir               = pdf_dir,
                db_path               = db_path,
                allow_scidownl        = allow_scidownl,
                config_path           = config_path,
                policy_clearance_path = policy_clearance_path,
                unpaywall_client      = unpaywall_client,
                openalex_client       = openalex_client,
                scidownl_fn           = scidownl_fn,
                dry_run               = dry_run,
            )
        except Exception as exc:
            print(f"\n  [phase5] ERROR on {ref_id}: {exc}", file=sys.stderr)
            if not dry_run:
                try:
                    mark_pdf_not_found(ref_id, db_path=db_path)
                except Exception:
                    pass
            status = PHASE5_STATUS_NOT_FOUND

        counts[status] += 1
        counts["total"] += 1

    print()
    print(
        f"[phase5] done. "
        f"pdf_acquired={counts[PHASE5_STATUS_ACQUIRED]}  "
        f"pdf_not_found={counts[PHASE5_STATUS_NOT_FOUND]}  "
        f"total={counts['total']}"
    )
    return counts


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Phase 5 PDF Acquisition Cascade.\n"
            "Processes phase4d_decision=ACCEPT rows only.\n"
            "Cascade: Unpaywall -> OpenAlex OA -> [Phase5B gate] -> scidownl"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", default=LIFECYCLE_DB,
                        help=f"lifecycle DB (default: {LIFECYCLE_DB})")
    parser.add_argument("--pdf-dir", default=DEFAULT_PDF_DIR,
                        help=f"PDF output directory (default: {DEFAULT_PDF_DIR})")
    parser.add_argument("--allow-scidownl", action="store_true",
                        help="Bypass Phase 5B gate (development only)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Simulate without writing to DB or disk")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process at most N rows")
    args = parser.parse_args()

    run_phase5_acquisition(
        args.db,
        pdf_dir        = args.pdf_dir,
        allow_scidownl = args.allow_scidownl,
        dry_run        = args.dry_run,
        limit          = args.limit,
    )


if __name__ == "__main__":
    main()
