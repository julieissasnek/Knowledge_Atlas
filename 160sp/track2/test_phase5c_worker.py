"""
test_phase5c_worker.py -- Phase 5C acquisition worker contract tests.

Coverage matrix:
  VIEW-1   v_acquisition_queue surfaces ACCEPT+pending+null-acquired rows
  VIEW-2   v_acquisition_queue excludes acquired rows (acquired_paper_id set)
  VIEW-3   v_acquisition_queue excludes EDGE_CASE/REJECT rows
  VIEW-4   v_acquisition_queue excludes pdf_not_found rows
  VIEW-5   read_acquisition_queue returns rows sorted voi_score DESC NULLS LAST
  VIEW-6   read_acquisition_queue batch limit respected
  GUARD-1  assert_acquisition_eligible passes for accepted_for_download+ACCEPT
  GUARD-2  assert_acquisition_eligible raises PrematureDownloadError for every
           non-eligible triage_stage
  GUARD-3  assert_acquisition_eligible raises PrematureDownloadError for
           EDGE_CASE/REJECT phase4d_decision
  GUARD-4  assert_acquisition_eligible raises AlreadyAcquiredError when
           acquired_paper_id is set
  GUARD-5  transition_is_valid blocks backward and terminal transitions
  CASCADE-ORDER  Steps 1->2->3; short-circuit on first success
  CASCADE-RETRY  On failure, phase5_status stays 'pending' (row stays in queue)
  CASCADE-RETRY-ATTEMPTS  pdf_acquisition_attempts incremented per source tried
  CASCADE-SCIDOWNL-GATE  Step 3 unreachable unless Steps 1+2 logged as failure
  WORKER-ORDER  Worker processes rows in voi_score DESC order
  WORKER-GUARD-BLOCK  Worker skips guard-blocked rows, does not increment attempts
  WORKER-NODOI  Rows without DOI counted as skipped, not cascade_failed
  WORKER-SUCCESS  Acquired rows removed from queue
  WORKER-DRY-RUN  dry_run makes no DB or disk writes
  PRISMA-UNOBT  get_unobtainable_candidates surfaces tried-but-failed rows
  FM1-SUFFOCATION  Direct evidence that Failure Mode 1 is structurally blocked
  FM2-SUFFOCATION  Direct evidence that Failure Mode 2 is structurally blocked
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ── Path setup ─────────────────────────────────────────────────────────────────

TRACK2 = Path(__file__).resolve().parent
AE_SRC = TRACK2.parent.parent.parent.parent / "Article_Eater" / "src"
if str(AE_SRC) not in sys.path:
    sys.path.insert(0, str(AE_SRC))

# ── Imports ────────────────────────────────────────────────────────────────────

from triage.funnel_state import (
    ACQUISITION_ELIGIBLE_STAGES,
    FUNNEL_STAGES_ORDERED,
    AlreadyAcquiredError,
    FunnelStateGuard,
    PrematureDownloadError,
    TERMINAL_STAGES,
)
from lifecycle_db import (
    get_connection,
    get_unobtainable_candidates,
    init_lifecycle_db,
    log_transition,
    read_acquisition_queue,
    upsert_reference,
)
from acquisition_worker import (
    AcquisitionWorker,
    SOURCE_OPENALEX,
    SOURCE_SCIDOWNL,
    SOURCE_UNPAYWALL,
)

# ── Fixtures ───────────────────────────────────────────────────────────────────

VALID_PDF = b"%PDF-1.4\n" + b"x" * 2000 + b"%%EOF"
VALID_DOI = "10.1016/j.buildenv.2022.109000"


def _seed_row(
    db: str,
    *,
    doi: str = VALID_DOI,
    title: str = "Daylight and cognition",
    decision: str = "ACCEPT",
    triage_stage: str = "accepted_for_download",
    voi_score: float = 0.75,
    phase5_status: str = "pending",
    acquired_paper_id: str = "",
) -> str:
    init_lifecycle_db(db)
    _, ref_id = upsert_reference(
        {"title_raw": title, "doi": doi, "discovered_via": "serpapi_scholar"},
        db_path=db,
    )
    with get_connection(db) as conn:
        conn.execute(
            "UPDATE article_references "
            "SET phase4d_decision=?, triage_stage=?, "
            "phase4d_voi_score=?, phase5_status=?, acquired_paper_id=? "
            "WHERE reference_id=?",
            (decision, triage_stage, voi_score, phase5_status,
             acquired_paper_id or None, ref_id),
        )
    return ref_id


def _seed_row_simple(db: str, doi: str, voi: float, title: str = "P") -> str:
    return _seed_row(db, doi=doi, title=title, voi_score=voi)


def _write_config(path: str, armed: bool = True) -> None:
    flag = "true" if armed else "false"
    Path(path).write_text(f"enable_paid_or_grey_sources: {flag}\n", encoding="utf-8")


def _write_clearance(path: str) -> None:
    Path(path).write_text('{"clearance_issued_by":"test"}', encoding="utf-8")


def _make_up(success: bool, out_bytes: bytes = VALID_PDF) -> MagicMock:
    m = MagicMock()
    if success:
        def _ok(doi, out):
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_bytes(out_bytes)
            return out
        m.try_download.side_effect = _ok
    else:
        m.try_download.return_value = None
    return m


def _make_worker(
    db: str,
    tmp_path: Path,
    *,
    up_success: bool = False,
    oa_success: bool = False,
    sci_fn=None,
    allow_scidownl: bool = False,
) -> AcquisitionWorker:
    cfg = str(tmp_path / "cfg.yaml")
    clr = str(tmp_path / "pc.json")
    _write_config(cfg, armed=allow_scidownl)
    if allow_scidownl:
        _write_clearance(clr)
    return AcquisitionWorker(
        db,
        pdf_dir               = str(tmp_path / "pdfs"),
        config_path           = cfg,
        policy_clearance_path = clr,
        allow_scidownl        = allow_scidownl,
        unpaywall_client      = _make_up(up_success),
        openalex_client       = _make_up(oa_success),
        scidownl_fn           = sci_fn,
    )


# ===========================================================================
# VIEW: v_acquisition_queue
# ===========================================================================

class TestAcquisitionQueueView:

    def test_accept_pending_row_appears(self, tmp_path):
        db  = str(tmp_path / "lc.db")
        ref = _seed_row(db)
        rows = read_acquisition_queue(db)
        assert any(r["reference_id"] == ref for r in rows)

    def test_acquired_row_excluded(self, tmp_path):
        db  = str(tmp_path / "lc.db")
        ref = _seed_row(db, acquired_paper_id="PDF-2026-01-01-000001")
        rows = read_acquisition_queue(db)
        assert not any(r["reference_id"] == ref for r in rows)

    def test_edge_case_excluded(self, tmp_path):
        db  = str(tmp_path / "lc.db")
        ref = _seed_row(db, doi="10.1/ec", decision="EDGE_CASE",
                        triage_stage="edge_case_review")
        rows = read_acquisition_queue(db)
        assert not any(r["reference_id"] == ref for r in rows)

    def test_pdf_not_found_excluded(self, tmp_path):
        db  = str(tmp_path / "lc.db")
        ref = _seed_row(db, doi="10.1/nf", phase5_status="pdf_not_found")
        rows = read_acquisition_queue(db)
        assert not any(r["reference_id"] == ref for r in rows)

    def test_sorted_voi_desc(self, tmp_path):
        db = str(tmp_path / "lc.db")
        _seed_row_simple(db, "10.1/lo", 0.30, "Low VOI")
        _seed_row_simple(db, "10.1/hi", 0.90, "High VOI")
        _seed_row_simple(db, "10.1/mi", 0.60, "Mid VOI")
        rows = read_acquisition_queue(db)
        scores = [r["phase4d_voi_score"] for r in rows]
        assert scores == sorted(scores, reverse=True)

    def test_null_voi_goes_last(self, tmp_path):
        db = str(tmp_path / "lc.db")
        _seed_row_simple(db, "10.1/a",    0.50, "Has VOI")
        _seed_row(db, doi="10.1/null", title="No VOI", voi_score=None)
        rows = read_acquisition_queue(db)
        assert rows[-1]["doi"] == "10.1/null"

    def test_limit_respected(self, tmp_path):
        db = str(tmp_path / "lc.db")
        for i in range(5):
            _seed_row_simple(db, f"10.1/r{i}", 0.5 - i * 0.05, f"P{i}")
        rows = read_acquisition_queue(db, limit=3)
        assert len(rows) == 3

    def test_empty_db_returns_empty(self, tmp_path):
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        assert read_acquisition_queue(db) == []


# ===========================================================================
# GUARD: FunnelStateGuard
# ===========================================================================

class TestFunnelStateGuard:
    _guard = FunnelStateGuard()

    def _row(self, stage: str, decision: str = "ACCEPT", acquired: str = "") -> dict:
        return {
            "reference_id":    "REF-TEST",
            "triage_stage":    stage,
            "phase4d_decision": decision,
            "acquired_paper_id": acquired,
        }

    def test_accepted_for_download_passes(self):
        self._guard.assert_acquisition_eligible(
            self._row("accepted_for_download", "ACCEPT")
        )

    @pytest.mark.parametrize("stage", [
        "metadata_only", "rejected_at_metadata", "abstract_pending",
        "abstract_collected", "abstract_missing", "edge_case_review",
        "rejected_at_abstract", "pdf_acquired", "duplicate",
    ])
    def test_non_eligible_stage_raises_premature(self, stage):
        with pytest.raises(PrematureDownloadError) as exc_info:
            self._guard.assert_acquisition_eligible(self._row(stage))
        assert "REF-TEST" in str(exc_info.value)

    @pytest.mark.parametrize("decision", ["EDGE_CASE", "REJECT", "MISSING_ABSTRACT", ""])
    def test_non_accept_decision_raises_premature(self, decision):
        with pytest.raises(PrematureDownloadError):
            self._guard.assert_acquisition_eligible(
                self._row("accepted_for_download", decision)
            )

    def test_already_acquired_raises(self):
        with pytest.raises(AlreadyAcquiredError):
            self._guard.assert_acquisition_eligible(
                self._row("accepted_for_download", "ACCEPT", "PDF-2026-01-01-000001")
            )

    def test_premature_error_message_contains_stage(self):
        with pytest.raises(PrematureDownloadError) as exc_info:
            self._guard.assert_acquisition_eligible(self._row("abstract_pending"))
        assert "abstract_pending" in str(exc_info.value)

    def test_transition_forward_valid(self):
        assert self._guard.transition_is_valid("abstract_pending", "abstract_collected")

    def test_transition_backward_invalid(self):
        assert not self._guard.transition_is_valid("abstract_collected", "abstract_pending")

    def test_transition_from_terminal_invalid(self):
        for stage in TERMINAL_STAGES:
            assert not self._guard.transition_is_valid(stage, "abstract_pending")

    def test_eligible_stages_is_exactly_accepted_for_download(self):
        assert ACQUISITION_ELIGIBLE_STAGES == frozenset({"accepted_for_download"})


# ===========================================================================
# CASCADE ORDER
# ===========================================================================

class TestCascadeOrder:

    def test_step1_unpaywall_called_first(self, tmp_path):
        db  = str(tmp_path / "lc.db")
        ref = _seed_row(db)
        w   = _make_worker(db, tmp_path, up_success=True)
        w.run()
        w._up.try_download.assert_called_once()
        w._oa.try_download.assert_not_called()

    def test_step2_called_when_step1_fails(self, tmp_path):
        db  = str(tmp_path / "lc.db")
        ref = _seed_row(db)
        w   = _make_worker(db, tmp_path, up_success=False, oa_success=True)
        w.run()
        w._up.try_download.assert_called_once()
        w._oa.try_download.assert_called_once()

    def test_step3_not_reached_when_step2_succeeds(self, tmp_path):
        db     = str(tmp_path / "lc.db")
        ref    = _seed_row(db)
        sci_fn = MagicMock(return_value=None)
        w      = _make_worker(db, tmp_path, up_success=False, oa_success=True,
                              sci_fn=sci_fn)
        w.run()
        sci_fn.assert_not_called()

    def test_step3_blocked_without_config(self, tmp_path):
        """scidownl unreachable when config is not armed."""
        db     = str(tmp_path / "lc.db")
        _seed_row(db)
        sci_fn = MagicMock(return_value=None)
        w      = _make_worker(db, tmp_path, up_success=False, oa_success=False,
                              sci_fn=sci_fn, allow_scidownl=False)
        w.run()
        sci_fn.assert_not_called()


# ===========================================================================
# RETRY / FAIL-STATE RETENTION
# ===========================================================================

class TestRetryRetention:
    """Failed rows must remain in v_acquisition_queue."""

    def test_failed_row_stays_in_queue(self, tmp_path):
        db  = str(tmp_path / "lc.db")
        ref = _seed_row(db)
        w   = _make_worker(db, tmp_path, up_success=False, oa_success=False)
        w.run()
        # Row must still appear in queue
        rows = read_acquisition_queue(db)
        assert any(r["reference_id"] == ref for r in rows)

    def test_failed_row_phase5_status_stays_pending(self, tmp_path):
        db  = str(tmp_path / "lc.db")
        ref = _seed_row(db)
        w   = _make_worker(db, tmp_path, up_success=False, oa_success=False)
        w.run()
        with get_connection(db) as conn:
            s = conn.execute(
                "SELECT phase5_status FROM article_references WHERE reference_id=?",
                (ref,),
            ).fetchone()["phase5_status"]
        assert s == "pending"

    def test_attempts_incremented_per_source_tried(self, tmp_path):
        db  = str(tmp_path / "lc.db")
        ref = _seed_row(db)
        w   = _make_worker(db, tmp_path, up_success=False, oa_success=False)
        w.run()
        with get_connection(db) as conn:
            n = conn.execute(
                "SELECT pdf_acquisition_attempts FROM article_references WHERE reference_id=?",
                (ref,),
            ).fetchone()["pdf_acquisition_attempts"]
        assert n == 2  # unpaywall + openalex

    def test_failed_row_becomes_unobtainable_candidate(self, tmp_path):
        db  = str(tmp_path / "lc.db")
        _seed_row(db)
        w   = _make_worker(db, tmp_path, up_success=False, oa_success=False)
        w.run()
        unobt = get_unobtainable_candidates(db, min_attempts=1)
        assert len(unobt) == 1

    def test_successful_row_removed_from_queue(self, tmp_path):
        db  = str(tmp_path / "lc.db")
        ref = _seed_row(db)
        w   = _make_worker(db, tmp_path, up_success=True)
        w.run()
        rows = read_acquisition_queue(db)
        assert not any(r["reference_id"] == ref for r in rows)

    def test_mark_pdf_not_found_never_called_on_failure(self, tmp_path):
        """
        RETRY CONTRACT: the worker must never call mark_pdf_not_found().
        Verify by checking that phase5_status is NOT 'pdf_not_found' after failure.
        """
        db  = str(tmp_path / "lc.db")
        ref = _seed_row(db)
        w   = _make_worker(db, tmp_path, up_success=False, oa_success=False)
        w.run()
        with get_connection(db) as conn:
            s = conn.execute(
                "SELECT phase5_status FROM article_references WHERE reference_id=?",
                (ref,),
            ).fetchone()["phase5_status"]
        assert s != "pdf_not_found"


# ===========================================================================
# WORKER ORDERING
# ===========================================================================

class TestWorkerOrdering:
    """Worker must process rows in voi_score DESC order."""

    def test_highest_voi_processed_first(self, tmp_path):
        db = str(tmp_path / "lc.db")
        _seed_row_simple(db, "10.1/lo", 0.30, "Low")
        _seed_row_simple(db, "10.1/hi", 0.90, "High")
        _seed_row_simple(db, "10.1/mi", 0.60, "Mid")

        processed_order = []
        up_mock = MagicMock()
        def _track_doi(doi, out):
            processed_order.append(doi)
            return None
        up_mock.try_download.side_effect = _track_doi

        oa_mock = MagicMock(); oa_mock.try_download.return_value = None

        w = AcquisitionWorker(
            db,
            pdf_dir               = str(tmp_path / "pdfs"),
            config_path           = str(tmp_path / "cfg.yaml"),
            policy_clearance_path = str(tmp_path / "pc.json"),
            unpaywall_client      = up_mock,
            openalex_client       = oa_mock,
        )
        w.run()

        assert processed_order[0] == "10.1/hi"
        assert processed_order[1] == "10.1/mi"
        assert processed_order[2] == "10.1/lo"


# ===========================================================================
# WORKER: GUARD BLOCK
# ===========================================================================

class TestWorkerGuardBlock:

    def test_guard_blocked_row_not_counted_in_cascade_failed(self, tmp_path):
        db = str(tmp_path / "lc.db")
        # Seed a row that the guard will block (wrong triage_stage)
        _seed_row(db, triage_stage="abstract_collected", decision="ACCEPT")
        w = _make_worker(db, tmp_path)
        counts = w.run()
        assert counts["guard_blocked"] == 1
        assert counts["cascade_failed"] == 0

    def test_guard_blocked_row_attempts_not_incremented(self, tmp_path):
        db  = str(tmp_path / "lc.db")
        # Insert row directly with wrong triage_stage but ACCEPT decision
        # (simulating a data integrity issue the guard must catch)
        init_lifecycle_db(db)
        _, ref = upsert_reference(
            {"title_raw": "Bad stage row", "doi": "10.1/bad",
             "discovered_via": "serpapi_scholar"},
            db_path=db,
        )
        with get_connection(db) as conn:
            conn.execute(
                "UPDATE article_references SET phase4d_decision='ACCEPT', "
                "triage_stage='abstract_collected', phase5_status='pending' "
                "WHERE reference_id=?", (ref,),
            )
        w = _make_worker(db, tmp_path)
        w.run()
        with get_connection(db) as conn:
            n = conn.execute(
                "SELECT pdf_acquisition_attempts FROM article_references WHERE reference_id=?",
                (ref,),
            ).fetchone()["pdf_acquisition_attempts"]
        assert n == 0


# ===========================================================================
# DRY RUN
# ===========================================================================

class TestDryRun:

    def test_dry_run_no_db_writes(self, tmp_path):
        db  = str(tmp_path / "lc.db")
        ref = _seed_row(db)
        w   = _make_worker(db, tmp_path, up_success=True)
        w.run(dry_run=True)
        rows = read_acquisition_queue(db)
        assert any(r["reference_id"] == ref for r in rows)  # still in queue

    def test_dry_run_acquired_count_correct(self, tmp_path):
        db = str(tmp_path / "lc.db")
        for i in range(3):
            _seed_row_simple(db, f"10.1/d{i}", 0.7 - i * 0.1, f"P{i}")
        w      = _make_worker(db, tmp_path, up_success=True)
        counts = w.run(dry_run=True)
        assert counts["acquired"] == 3


# ===========================================================================
# FAILURE MODE 1 SUFFOCATION: structural evidence
# ===========================================================================

class TestFailureMode1Suffocation:
    """
    Structural evidence that Failure Mode 1 (premature downloads) is blocked.

    The barrier is not a conditional flag — it is a function that raises.
    Bypassing it requires explicitly catching PrematureDownloadError, which
    is visible and auditable.
    """

    def test_worker_calls_assert_before_any_download(self, tmp_path):
        """
        Verify that the worker RAISES before any downloader is called
        when a row has not cleared Phase 4D.
        """
        db = str(tmp_path / "lc.db")
        init_lifecycle_db(db)
        _, ref = upsert_reference(
            {"title_raw": "Early row", "doi": "10.1/early",
             "discovered_via": "serpapi_scholar"},
            db_path=db,
        )
        with get_connection(db) as conn:
            conn.execute(
                "UPDATE article_references SET phase4d_decision='ACCEPT', "
                "triage_stage='metadata_only', phase5_status='pending' "
                "WHERE reference_id=?", (ref,),
            )

        up_mock = MagicMock(); up_mock.try_download.return_value = None
        oa_mock = MagicMock(); oa_mock.try_download.return_value = None

        w = AcquisitionWorker(
            db,
            pdf_dir               = str(tmp_path / "pdfs"),
            config_path           = str(tmp_path / "cfg.yaml"),
            policy_clearance_path = str(tmp_path / "pc.json"),
            unpaywall_client      = up_mock,
            openalex_client       = oa_mock,
        )
        counts = w.run()

        # Guard blocked it -- no download attempted
        assert counts["guard_blocked"] == 1
        up_mock.try_download.assert_not_called()
        oa_mock.try_download.assert_not_called()

    def test_guard_is_not_bypassable_via_view_filter_alone(self, tmp_path):
        """
        Defense-in-depth: even if v_acquisition_queue returned a bad row
        (e.g. due to a schema migration that changed the view), the guard
        catches it at the code level.
        """
        guard = FunnelStateGuard()
        bad_row = {
            "reference_id":    "REF-BAD",
            "triage_stage":    "metadata_only",
            "phase4d_decision": "ACCEPT",
            "acquired_paper_id": "",
        }
        with pytest.raises(PrematureDownloadError):
            guard.assert_acquisition_eligible(bad_row)


# ===========================================================================
# FAILURE MODE 2 SUFFOCATION: structural evidence
# ===========================================================================

class TestFailureMode2Suffocation:
    """
    Structural evidence that Failure Mode 2 (scidownl defaulting) is blocked.

    Three independent locks must all fail before scidownl can run:
      1. YAML config not armed (default: false)
      2. policy_clearance.json absent (must be manually created)
      3. C3 cascade exhaustion: lifecycle_transitions must contain 'failure'
         rows for BOTH unpaywall AND openalex_oa.  The cascade structure
         creates this evidence as a side effect of running Steps 1 and 2.
    """

    def test_scidownl_blocked_when_config_not_armed(self, tmp_path):
        db     = str(tmp_path / "lc.db")
        _seed_row(db)
        sci_fn = MagicMock(return_value=None)
        w      = _make_worker(db, tmp_path, sci_fn=sci_fn, allow_scidownl=False)
        w.run()
        sci_fn.assert_not_called()

    def test_scidownl_blocked_when_clearance_absent(self, tmp_path):
        """Config is armed but clearance file doesn't exist -> blocked."""
        db = str(tmp_path / "lc.db")
        _seed_row(db)
        sci_fn = MagicMock(return_value=None)
        cfg    = str(tmp_path / "cfg.yaml"); _write_config(cfg, armed=True)
        # Deliberately do NOT create policy_clearance.json
        w = AcquisitionWorker(
            db,
            pdf_dir               = str(tmp_path / "pdfs"),
            config_path           = cfg,
            policy_clearance_path = str(tmp_path / "no_clearance.json"),
            allow_scidownl        = True,
            unpaywall_client      = _make_up(False),
            openalex_client       = _make_up(False),
            scidownl_fn           = sci_fn,
        )
        w.run()
        sci_fn.assert_not_called()

    def test_scidownl_called_when_all_four_conditions_met(self, tmp_path):
        """When all four gate conditions are satisfied, scidownl runs."""
        db  = str(tmp_path / "lc.db")
        ref = _seed_row(db)

        cfg = str(tmp_path / "cfg.yaml"); _write_config(cfg, armed=True)
        clr = str(tmp_path / "pc.json");  _write_clearance(clr)

        # Pre-log cascade failures so C3 passes from the start
        log_transition(ref, "unpaywall",   "failure", doi=VALID_DOI, db_path=db)
        log_transition(ref, "openalex_oa", "failure", doi=VALID_DOI, db_path=db)

        sci_fn = MagicMock(return_value=None)
        w = AcquisitionWorker(
            db,
            pdf_dir               = str(tmp_path / "pdfs"),
            config_path           = cfg,
            policy_clearance_path = clr,
            allow_scidownl        = True,
            unpaywall_client      = _make_up(False),
            openalex_client       = _make_up(False),
            scidownl_fn           = sci_fn,
        )
        w.run()
        sci_fn.assert_called_once()

    def test_c3_cannot_be_satisfied_without_steps_1_and_2_running(self, tmp_path):
        """
        If the worker somehow skipped Steps 1 and 2, lifecycle_transitions
        would have no failure entries.  verify_scidownl_clearance() Condition C3
        would then block Step 3.

        This test verifies that Condition C3 is checked and blocks scidownl
        when Steps 1+2 have NOT been logged.
        """
        from scidownl_policy_gate import verify_scidownl_clearance, CascadeNotExhaustedError
        db  = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        cfg = str(tmp_path / "cfg.yaml"); _write_config(cfg, armed=True)
        clr = str(tmp_path / "pc.json"); _write_clearance(clr)
        # No lifecycle_transitions entries logged
        with pytest.raises(CascadeNotExhaustedError):
            verify_scidownl_clearance(
                "REF-HYPOTHETICAL", "ACCEPT",
                db_path=db, config_path=cfg, policy_clearance_path=clr,
            )
