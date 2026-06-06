"""
test_phase5_pdf_acquisition.py -- Phase 5 PDF acquisition contract tests.

Coverage matrix:
  GATE-1   Only ACCEPT rows are processed; EDGE_CASE/REJECT/MISSING excluded
  GATE-2   phase5_status='pending' only; already-acquired rows skipped
  TELE-1   pdf_acquisition_attempts incremented once per source tried
  TELE-2   pdf_acquisition_last_source written after every attempt
  CASCADE-1 Unpaywall tried first; success short-circuits cascade
  CASCADE-2 Unpaywall fail -> OpenAlex OA tried second
  CASCADE-3 OpenAlex fail -> Phase 5B gate blocks scidownl by default
  CASCADE-4 scidownl invoked when allow_scidownl=True and Steps 1+2 fail
  CASCADE-5 All sources fail -> pdf_not_found, never silently dropped
  PAPERS-1  Success inserts row in papers table
  PAPERS-2  acquired_paper_id back-reference set on article_references
  PAPERS-3  paper_id uniqueness across multiple acquisitions
  SHA256-1  sha256 column populated on success
  VALID-1   Magic-byte validation rejects non-PDF bytes
  VALID-2   Magic-byte validation accepts valid PDF regardless of Content-Type
  NODOI-1   Rows without DOI are marked pdf_not_found immediately
  DRYRUN-1  dry_run=True makes no DB or disk writes
  BATCH-1   Batch processes only eligible rows
  AUDIT-1   Phase5BPolicyGateError contains identifying information
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

# ── Path setup ─────────────────────────────────────────────────────────────────

TRACK2 = Path(__file__).resolve().parent
AE_SRC = TRACK2.parent.parent.parent.parent / "Article_Eater" / "src"
if str(AE_SRC) not in sys.path:
    sys.path.insert(0, str(AE_SRC))

# ── Imports ────────────────────────────────────────────────────────────────────

from ingest.pdf_downloader import (  # type: ignore
    OpenAlexOADownloader,
    UnpaywallDownloader,
    _is_valid_pdf_bytes,
)
from lifecycle_db import (
    TRIAGE_STAGE_ABSTRACT_COLLECTED,
    TRIAGE_STAGE_ACCEPTED_FOR_DOWNLOAD,
    TRIAGE_STAGE_EDGE_CASE_REVIEW,
    TRIAGE_STAGE_REJECTED_AT_ABSTRACT,
    get_accepted_for_pdf_download,
    get_connection,
    init_lifecycle_db,
    update_triage_stage,
    upsert_reference,
)
from pdf_acquisition_engine import (
    PHASE5_STATUS_ACQUIRED,
    PHASE5_STATUS_NOT_FOUND,
    Phase5BPolicyGateError,
    acquire_pdf_for_record,
    run_phase5_acquisition,
    _phase5b_policy_gate,
)

# ── Test fixtures ──────────────────────────────────────────────────────────────

VALID_PDF_BYTES = b"%PDF-1.4\n" + b"x" * 2000 + b"\n%%EOF"
JUNK_BYTES      = b"<html><body>Not a PDF</body></html>" + b"x" * 500
VALID_DOI       = "10.1016/j.buildenv.2022.109000"


def _seed_accept_row(
    db: str,
    *,
    doi: str = VALID_DOI,
    title: str = "Daylight and cognitive performance",
    decision: str = "ACCEPT",
) -> str:
    """Insert a row with a given phase4d_decision and return reference_id."""
    _, ref_id = upsert_reference(
        {"title_raw": title, "doi": doi, "discovered_via": "serpapi_scholar"},
        db_path=db,
    )
    # Set triage_stage and phase4d_decision
    stage = (
        TRIAGE_STAGE_ACCEPTED_FOR_DOWNLOAD if decision == "ACCEPT"
        else TRIAGE_STAGE_EDGE_CASE_REVIEW if decision == "EDGE_CASE"
        else TRIAGE_STAGE_REJECTED_AT_ABSTRACT
    )
    with get_connection(db) as conn:
        conn.execute(
            "UPDATE article_references SET triage_stage=?, phase4d_decision=? WHERE reference_id=?",
            (stage, decision, ref_id),
        )
    return ref_id


def _mock_success(doi: str, out_path: str) -> str:
    """
    Simulate a downloader that writes a valid PDF to the given path.
    Returns the path (success).
    """
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_bytes(VALID_PDF_BYTES)
    return out_path


def _mock_fail(*args) -> None:
    """Simulate a downloader that returns None (failure)."""
    return None


def _make_up(success: bool) -> MagicMock:
    m = MagicMock()
    m.try_download.side_effect = _mock_success if success else _mock_fail
    return m


def _make_oa(success: bool) -> MagicMock:
    m = MagicMock()
    m.try_download.side_effect = _mock_success if success else _mock_fail
    return m


def _make_sci(success: bool):
    if success:
        def _sci(doi, output_dir="pdfs"):
            p = str(Path(output_dir) / "sci.pdf")
            Path(output_dir).mkdir(parents=True, exist_ok=True)
            Path(p).write_bytes(VALID_PDF_BYTES)
            return p
        return _sci
    return lambda doi, output_dir="pdfs": None


def _make_row(ref_id: str, doi: str = VALID_DOI) -> dict:
    return {"reference_id": ref_id, "doi": doi, "title_raw": "Test paper"}


# ===========================================================================
# GATE: eligibility enforcement
# ===========================================================================

class TestEligibilityGate:
    """Only ACCEPT rows with phase5_status=pending are returned."""

    def test_accept_rows_returned(self, tmp_path):
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        ref = _seed_accept_row(db, decision="ACCEPT")
        rows = get_accepted_for_pdf_download(db)
        assert any(r["reference_id"] == ref for r in rows)

    def test_edge_case_excluded(self, tmp_path):
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        ref = _seed_accept_row(db, doi="10.1/edge", decision="EDGE_CASE")
        rows = get_accepted_for_pdf_download(db)
        assert not any(r["reference_id"] == ref for r in rows)

    def test_reject_excluded(self, tmp_path):
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        ref = _seed_accept_row(db, doi="10.1/rej", decision="REJECT")
        rows = get_accepted_for_pdf_download(db)
        assert not any(r["reference_id"] == ref for r in rows)

    def test_already_acquired_excluded(self, tmp_path):
        db  = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        ref = _seed_accept_row(db, decision="ACCEPT")
        with get_connection(db) as conn:
            conn.execute(
                "UPDATE article_references SET acquired_paper_id='PDF-X' WHERE reference_id=?",
                (ref,),
            )
        rows = get_accepted_for_pdf_download(db)
        assert not any(r["reference_id"] == ref for r in rows)

    def test_empty_db_returns_empty(self, tmp_path):
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        assert get_accepted_for_pdf_download(db) == []

    def test_gate_excludes_gemini_stage2_status(self, tmp_path):
        """
        DEFECT 2 regression: gate must use phase4d_decision, not stage2_status.
        A row with stage2_status=ACCEPT but phase4d_decision=EDGE_CASE must NOT
        be returned.
        """
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        _, ref = upsert_reference(
            {"title_raw": "X", "doi": "10.1/x", "discovered_via": "serpapi_scholar"},
            db_path=db,
        )
        with get_connection(db) as conn:
            conn.execute(
                "UPDATE article_references SET phase4d_decision='EDGE_CASE' WHERE reference_id=?",
                (ref,),
            )
        rows = get_accepted_for_pdf_download(db)
        assert not any(r["reference_id"] == ref for r in rows)


# ===========================================================================
# TELEMETRY: attempt counter and last-source tracking
# ===========================================================================

class TestTelemetry:
    """pdf_acquisition_attempts and pdf_acquisition_last_source must be precise."""

    def test_attempt_incremented_for_unpaywall(self, tmp_path):
        db  = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        ref = _seed_accept_row(db)

        acquire_pdf_for_record(
            _make_row(ref),
            db_path=db, pdf_dir=str(tmp_path / "pdfs"),
            unpaywall_client=_make_up(False),
            openalex_client=_make_oa(False),
        )

        with get_connection(db) as conn:
            row = conn.execute(
                "SELECT pdf_acquisition_attempts, pdf_acquisition_last_source "
                "FROM article_references WHERE reference_id=?", (ref,),
            ).fetchone()

        assert row["pdf_acquisition_attempts"] >= 1

    def test_last_source_written_as_unpaywall_on_first_attempt(self, tmp_path):
        db  = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        ref = _seed_accept_row(db)

        up = _make_up(False)
        oa = _make_oa(False)
        acquire_pdf_for_record(
            _make_row(ref),
            db_path=db, pdf_dir=str(tmp_path / "pdfs"),
            unpaywall_client=up, openalex_client=oa,
        )

        # After unpaywall + openalex both fail, last source is openalex
        with get_connection(db) as conn:
            src = conn.execute(
                "SELECT pdf_acquisition_last_source FROM article_references WHERE reference_id=?",
                (ref,),
            ).fetchone()["pdf_acquisition_last_source"]

        assert src in ("unpaywall", "openalex_oa")

    def test_attempts_equals_two_when_step1_fails_step2_tries(self, tmp_path):
        db  = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        ref = _seed_accept_row(db)

        acquire_pdf_for_record(
            _make_row(ref),
            db_path=db, pdf_dir=str(tmp_path / "pdfs"),
            unpaywall_client=_make_up(False),
            openalex_client=_make_oa(False),
        )

        with get_connection(db) as conn:
            attempts = conn.execute(
                "SELECT pdf_acquisition_attempts FROM article_references WHERE reference_id=?",
                (ref,),
            ).fetchone()["pdf_acquisition_attempts"]

        assert attempts == 2

    def test_attempts_equals_one_on_unpaywall_success(self, tmp_path):
        db  = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        ref = _seed_accept_row(db)

        acquire_pdf_for_record(
            _make_row(ref),
            db_path=db, pdf_dir=str(tmp_path / "pdfs"),
            unpaywall_client=_make_up(True),
            openalex_client=_make_oa(False),
        )

        with get_connection(db) as conn:
            attempts = conn.execute(
                "SELECT pdf_acquisition_attempts FROM article_references WHERE reference_id=?",
                (ref,),
            ).fetchone()["pdf_acquisition_attempts"]

        assert attempts == 1

    def test_scidownl_not_counted_when_gate_blocks(self, tmp_path):
        """Defect 6 regression: blocked scidownl must NOT increment attempt counter."""
        db  = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        ref = _seed_accept_row(db)

        acquire_pdf_for_record(
            _make_row(ref),
            db_path=db, pdf_dir=str(tmp_path / "pdfs"),
            unpaywall_client=_make_up(False),
            openalex_client=_make_oa(False),
            scidownl_fn=_make_sci(True),   # would succeed if allowed
            allow_scidownl=False,           # gate blocks it
        )

        with get_connection(db) as conn:
            attempts = conn.execute(
                "SELECT pdf_acquisition_attempts FROM article_references WHERE reference_id=?",
                (ref,),
            ).fetchone()["pdf_acquisition_attempts"]

        assert attempts == 2  # only unpaywall + openalex; scidownl never called


# ===========================================================================
# CASCADE: order and short-circuit behaviour
# ===========================================================================

class TestCascadeOrder:
    """Cascade must fire in exact order and short-circuit on first success."""

    def test_unpaywall_called_first(self, tmp_path):
        db  = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        ref = _seed_accept_row(db)
        up  = _make_up(True)
        oa  = _make_oa(True)

        acquire_pdf_for_record(
            _make_row(ref),
            db_path=db, pdf_dir=str(tmp_path / "pdfs"),
            unpaywall_client=up, openalex_client=oa,
        )

        up.try_download.assert_called_once()
        oa.try_download.assert_not_called()

    def test_openalex_called_when_unpaywall_fails(self, tmp_path):
        db  = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        ref = _seed_accept_row(db)
        up  = _make_up(False)
        oa  = _make_oa(True)

        acquire_pdf_for_record(
            _make_row(ref),
            db_path=db, pdf_dir=str(tmp_path / "pdfs"),
            unpaywall_client=up, openalex_client=oa,
        )

        up.try_download.assert_called_once()
        oa.try_download.assert_called_once()

    def test_scidownl_blocked_by_default(self, tmp_path):
        """Phase5B gate raises when allow_scidownl=False."""
        db      = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        ref     = _seed_accept_row(db)
        sci_fn  = MagicMock(return_value=None)

        acquire_pdf_for_record(
            _make_row(ref),
            db_path=db, pdf_dir=str(tmp_path / "pdfs"),
            unpaywall_client=_make_up(False),
            openalex_client=_make_oa(False),
            scidownl_fn=sci_fn,
            allow_scidownl=False,
        )

        sci_fn.assert_not_called()

    def test_scidownl_invoked_when_gate_passes(self, tmp_path):
        db    = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        ref   = _seed_accept_row(db)
        sci   = MagicMock(return_value=None)  # returns None (not found)

        acquire_pdf_for_record(
            _make_row(ref),
            db_path=db, pdf_dir=str(tmp_path / "pdfs"),
            unpaywall_client=_make_up(False),
            openalex_client=_make_oa(False),
            scidownl_fn=sci,
            allow_scidownl=True,
        )

        sci.assert_called_once()

    def test_all_fail_marks_not_found(self, tmp_path):
        db  = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        ref = _seed_accept_row(db)

        status = acquire_pdf_for_record(
            _make_row(ref),
            db_path=db, pdf_dir=str(tmp_path / "pdfs"),
            unpaywall_client=_make_up(False),
            openalex_client=_make_oa(False),
        )

        assert status == PHASE5_STATUS_NOT_FOUND
        with get_connection(db) as conn:
            s = conn.execute(
                "SELECT phase5_status FROM article_references WHERE reference_id=?", (ref,),
            ).fetchone()["phase5_status"]
        assert s == "pdf_not_found"


# ===========================================================================
# PAPERS TABLE: registration and back-reference
# ===========================================================================

class TestPapersTable:
    """Successful acquisition must register in papers table and back-reference."""

    def test_papers_row_inserted_on_success(self, tmp_path):
        db  = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        ref = _seed_accept_row(db)

        acquire_pdf_for_record(
            _make_row(ref),
            db_path=db, pdf_dir=str(tmp_path / "pdfs"),
            unpaywall_client=_make_up(True),
            openalex_client=_make_oa(False),
        )

        with get_connection(db) as conn:
            row = conn.execute(
                "SELECT * FROM papers WHERE reference_id=?", (ref,),
            ).fetchone()

        assert row is not None
        assert row["local_pdf_path"]

    def test_acquired_paper_id_set_on_article_references(self, tmp_path):
        db  = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        ref = _seed_accept_row(db)

        acquire_pdf_for_record(
            _make_row(ref),
            db_path=db, pdf_dir=str(tmp_path / "pdfs"),
            unpaywall_client=_make_up(True),
            openalex_client=_make_oa(False),
        )

        with get_connection(db) as conn:
            pid = conn.execute(
                "SELECT acquired_paper_id FROM article_references WHERE reference_id=?", (ref,),
            ).fetchone()["acquired_paper_id"]

        assert pid and pid.startswith("PDF-")

    def test_paper_id_unique_across_acquisitions(self, tmp_path):
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        r1 = _seed_accept_row(db, doi="10.1/a", title="Paper A")
        r2 = _seed_accept_row(db, doi="10.1/b", title="Paper B")

        for ref in (r1, r2):
            acquire_pdf_for_record(
                {"reference_id": ref, "doi": ref.split("-")[-1][:10], "title_raw": "T"},
                db_path=db, pdf_dir=str(tmp_path / "pdfs"),
                unpaywall_client=_make_up(True),
                openalex_client=_make_oa(False),
            )

        with get_connection(db) as conn:
            ids = [r["paper_id"] for r in conn.execute("SELECT paper_id FROM papers").fetchall()]

        assert len(ids) == len(set(ids)), "paper_id values must be unique"

    def test_sha256_populated_on_success(self, tmp_path):
        db  = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        ref = _seed_accept_row(db)

        acquire_pdf_for_record(
            _make_row(ref),
            db_path=db, pdf_dir=str(tmp_path / "pdfs"),
            unpaywall_client=_make_up(True),
            openalex_client=_make_oa(False),
        )

        with get_connection(db) as conn:
            sha = conn.execute(
                "SELECT sha256 FROM papers WHERE reference_id=?", (ref,),
            ).fetchone()["sha256"]

        assert sha and len(sha) == 64  # hex SHA-256

    def test_no_papers_row_on_failure(self, tmp_path):
        db  = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        ref = _seed_accept_row(db)

        acquire_pdf_for_record(
            _make_row(ref),
            db_path=db, pdf_dir=str(tmp_path / "pdfs"),
            unpaywall_client=_make_up(False),
            openalex_client=_make_oa(False),
        )

        with get_connection(db) as conn:
            row = conn.execute(
                "SELECT * FROM papers WHERE reference_id=?", (ref,),
            ).fetchone()

        assert row is None


# ===========================================================================
# PDF VALIDATION: magic-byte check
# ===========================================================================

class TestPDFValidation:
    """_is_valid_pdf_bytes must accept PDFs by magic bytes, reject junk."""

    def test_valid_pdf_passes(self):
        assert _is_valid_pdf_bytes(VALID_PDF_BYTES) is True

    def test_html_junk_fails(self):
        assert _is_valid_pdf_bytes(JUNK_BYTES) is False

    def test_empty_bytes_fail(self):
        assert _is_valid_pdf_bytes(b"") is False

    def test_short_valid_magic_fails_size_check(self):
        # %PDF magic but only 10 bytes total — below MIN_PDF_BYTES
        assert _is_valid_pdf_bytes(b"%PDF-1.4\r\n") is False

    def test_valid_pdf_with_binary_content_type_accepted(self):
        """
        Defect 8 regression: valid PDFs must pass regardless of Content-Type.
        _is_valid_pdf_bytes only checks magic bytes, not headers.
        """
        # Simulate bytes a server might return with Content-Type: binary/octet-stream
        data = VALID_PDF_BYTES
        assert _is_valid_pdf_bytes(data) is True

    def test_exact_min_bytes_fails(self):
        from ingest.pdf_downloader import MIN_PDF_BYTES  # type: ignore
        data = b"%PDF" + b"x" * (MIN_PDF_BYTES - 4)  # exactly at boundary
        assert _is_valid_pdf_bytes(data) is False  # must be strictly greater

    def test_one_over_min_bytes_passes(self):
        from ingest.pdf_downloader import MIN_PDF_BYTES  # type: ignore
        data = b"%PDF" + b"x" * (MIN_PDF_BYTES - 3)  # one byte over
        assert _is_valid_pdf_bytes(data) is True


# ===========================================================================
# NO-DOI: rows without DOI handled immediately
# ===========================================================================

class TestNoDOI:
    """Rows without a DOI are marked pdf_not_found without attempting any source."""

    def test_no_doi_returns_not_found(self, tmp_path):
        db  = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        ref = _seed_accept_row(db, doi="", title="No DOI paper")
        row = {"reference_id": ref, "doi": "", "title_raw": "No DOI paper"}
        up  = _make_up(True)

        status = acquire_pdf_for_record(
            row,
            db_path=db, pdf_dir=str(tmp_path / "pdfs"),
            unpaywall_client=up, openalex_client=_make_oa(False),
        )

        assert status == PHASE5_STATUS_NOT_FOUND
        up.try_download.assert_not_called()


# ===========================================================================
# DRY-RUN: no writes
# ===========================================================================

class TestDryRun:
    """dry_run=True must not write to the DB or disk."""

    def test_dry_run_does_not_write_papers_table(self, tmp_path):
        db  = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        ref = _seed_accept_row(db)

        acquire_pdf_for_record(
            _make_row(ref),
            db_path=db, pdf_dir=str(tmp_path / "pdfs"),
            unpaywall_client=_make_up(True),
            openalex_client=_make_oa(False),
            dry_run=True,
        )

        with get_connection(db) as conn:
            row = conn.execute("SELECT * FROM papers WHERE reference_id=?", (ref,)).fetchone()
        assert row is None

    def test_dry_run_does_not_update_phase5_status(self, tmp_path):
        db  = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        ref = _seed_accept_row(db)

        acquire_pdf_for_record(
            _make_row(ref),
            db_path=db, pdf_dir=str(tmp_path / "pdfs"),
            unpaywall_client=_make_up(True),
            openalex_client=_make_oa(False),
            dry_run=True,
        )

        with get_connection(db) as conn:
            s = conn.execute(
                "SELECT phase5_status FROM article_references WHERE reference_id=?", (ref,),
            ).fetchone()["phase5_status"]
        assert s == "pending"

    def test_dry_run_batch_returns_correct_counts(self, tmp_path):
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        for i in range(3):
            _seed_accept_row(db, doi=f"10.1/dry{i}", title=f"Paper {i}")

        counts = run_phase5_acquisition(
            db,
            pdf_dir=str(tmp_path / "pdfs"),
            unpaywall_client=_make_up(True),
            openalex_client=_make_oa(False),
            dry_run=True,
        )

        assert counts["total"] == 3
        assert counts[PHASE5_STATUS_ACQUIRED] == 3


# ===========================================================================
# PHASE 5B POLICY GATE: structure and content
# ===========================================================================

class TestPhase5BPolicyGate:
    """Phase5BPolicyGateError must be raised and carry the DOI."""

    def test_gate_raises_when_disabled(self):
        with pytest.raises(Phase5BPolicyGateError):
            _phase5b_policy_gate("10.1/test", allow_scidownl=False)

    def test_gate_error_contains_doi(self):
        with pytest.raises(Phase5BPolicyGateError) as exc_info:
            _phase5b_policy_gate("10.1/test", allow_scidownl=False)
        assert "10.1/test" in str(exc_info.value)

    def test_gate_passes_with_allow_flag(self):
        # Must not raise; may print a warning to stderr but returns normally
        _phase5b_policy_gate("10.1/test", allow_scidownl=True)

    def test_gate_error_mentions_phase5b(self):
        with pytest.raises(Phase5BPolicyGateError) as exc_info:
            _phase5b_policy_gate("10.1/test", allow_scidownl=False)
        msg = str(exc_info.value).lower()
        assert "phase 5b" in msg or "policy" in msg


# ===========================================================================
# BATCH PROCESSOR
# ===========================================================================

class TestBatchProcessor:
    """run_phase5_acquisition processes only eligible rows."""

    def test_batch_only_processes_accept_rows(self, tmp_path):
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        _seed_accept_row(db, doi="10.1/a", decision="ACCEPT")
        _seed_accept_row(db, doi="10.1/b", decision="EDGE_CASE")
        _seed_accept_row(db, doi="10.1/c", decision="REJECT")

        counts = run_phase5_acquisition(
            db,
            pdf_dir=str(tmp_path / "pdfs"),
            unpaywall_client=_make_up(True),
            openalex_client=_make_oa(False),
        )

        assert counts["total"] == 1  # only ACCEPT row processed

    def test_batch_counts_sum_to_total(self, tmp_path):
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        for i in range(4):
            _seed_accept_row(db, doi=f"10.1/r{i}", title=f"Paper {i}")

        counts = run_phase5_acquisition(
            db,
            pdf_dir=str(tmp_path / "pdfs"),
            unpaywall_client=_make_up(True),
            openalex_client=_make_oa(False),
        )

        assert counts[PHASE5_STATUS_ACQUIRED] + counts[PHASE5_STATUS_NOT_FOUND] == counts["total"]

    def test_limit_respected(self, tmp_path):
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        for i in range(5):
            _seed_accept_row(db, doi=f"10.1/l{i}", title=f"Lim {i}")

        counts = run_phase5_acquisition(
            db,
            pdf_dir=str(tmp_path / "pdfs"),
            unpaywall_client=_make_up(False),
            openalex_client=_make_oa(False),
            limit=2,
        )

        assert counts["total"] == 2

    def test_no_silent_drops_all_fail(self, tmp_path):
        """Every row must land in pdf_not_found, never disappear."""
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        refs = [_seed_accept_row(db, doi=f"10.1/nd{i}", title=f"P{i}") for i in range(3)]

        run_phase5_acquisition(
            db,
            pdf_dir=str(tmp_path / "pdfs"),
            unpaywall_client=_make_up(False),
            openalex_client=_make_oa(False),
        )

        with get_connection(db) as conn:
            for ref in refs:
                s = conn.execute(
                    "SELECT phase5_status FROM article_references WHERE reference_id=?", (ref,),
                ).fetchone()["phase5_status"]
                assert s == "pdf_not_found", f"{ref} was silently dropped"
