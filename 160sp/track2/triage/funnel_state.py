"""
triage/funnel_state.py -- Authoritative funnel state machine for the
Knowledge Atlas PDF acquisition pipeline.

This module is the single source of truth for:
  - The complete ordered sequence of triage_stage values
  - Which stages are eligible for PDF acquisition
  - Which stages are terminal (no further processing)
  - The FunnelStateGuard class that the worker binds to as a structural barrier

STRUCTURAL BARRIER AGAINST FAILURE MODE 1: PREMATURE DOWNLOADS
==============================================================
acquisition_worker.AcquisitionWorker imports FunnelStateGuard and calls
assert_acquisition_eligible() on EVERY row before any download code runs.
If the row has not passed through Phase 4D and received triage_stage =
'accepted_for_download', the guard raises PrematureDownloadError and the
worker skips that row without touching any files or network connections.

This makes it structurally impossible for the worker to download a PDF for
a paper that has not completed the full triage funnel. The check is not a
conditional -- it is an invariant enforced at the function-call level.

Funnel stage sequence
---------------------
  metadata_only          -> Phase 4A input (raw insert)
  rejected_at_metadata   -> Phase 4A hard-stop [TERMINAL]
  abstract_pending       -> Phase 4A survivor; abstract not yet fetched
  abstract_collected     -> Phase 4B success; abstract stored
  abstract_missing       -> Phase 4B exhaustion [TERMINAL]
  accepted_for_download  -> Phase 4D ACCEPT  <-- ONLY valid input to Phase 5
  edge_case_review       -> Phase 4D EDGE_CASE
  rejected_at_abstract   -> Phase 4D REJECT or MISSING_ABSTRACT [TERMINAL]
  pdf_acquired           -> Phase 5 success [TERMINAL]
  duplicate              -> Dedup marker [TERMINAL]
"""
from __future__ import annotations

from typing import Optional

# ── Stage registry ────────────────────────────────────────────────────────────

FUNNEL_STAGES_ORDERED: tuple[str, ...] = (
    "metadata_only",
    "rejected_at_metadata",
    "abstract_pending",
    "abstract_collected",
    "abstract_missing",
    "accepted_for_download",
    "edge_case_review",
    "rejected_at_abstract",
    "pdf_acquired",
    "duplicate",
)

ALL_STAGES: frozenset[str] = frozenset(FUNNEL_STAGES_ORDERED)

# The ONLY stage from which PDF acquisition may proceed.
# Any other value raises PrematureDownloadError.
ACQUISITION_ELIGIBLE_STAGES: frozenset[str] = frozenset({
    "accepted_for_download",
})

# Stages where no further processing should occur.
TERMINAL_STAGES: frozenset[str] = frozenset({
    "rejected_at_metadata",
    "abstract_missing",
    "rejected_at_abstract",
    "pdf_acquired",
    "duplicate",
})

# Phase4D decisions that are eligible for acquisition
ACQUISITION_ELIGIBLE_DECISIONS: frozenset[str] = frozenset({"ACCEPT"})


# ── Exceptions ────────────────────────────────────────────────────────────────

class FunnelStateError(RuntimeError):
    """Base class for all funnel state violations."""

    def __init__(self, reference_id: str, message: str) -> None:
        self.reference_id = reference_id
        super().__init__(f"[FunnelStateError] {reference_id}: {message}")


class PrematureDownloadError(FunnelStateError):
    """
    Raised when PDF acquisition is attempted on a row that has not passed
    through Phase 4D and reached triage_stage = 'accepted_for_download'.

    This is the structural barrier against Failure Mode 1.

    Possible causes:
      - Row is still in 'metadata_only', 'abstract_pending', etc.
      - Row reached Phase 4D but was classified as EDGE_CASE, REJECT,
        or MISSING_ABSTRACT (triage_stage not 'accepted_for_download')
      - Row was already acquired (triage_stage = 'pdf_acquired')
      - Duplicate row attempting to re-enter the acquisition path
    """


class AlreadyAcquiredError(FunnelStateError):
    """
    Raised when acquisition is attempted on a row that already has an
    acquired_paper_id.  This prevents double-downloading.
    """


class TerminalStageError(FunnelStateError):
    """Raised when processing is attempted on a terminal-stage row."""


# ── Guard ─────────────────────────────────────────────────────────────────────

class FunnelStateGuard:
    """
    Structural barrier against premature PDF acquisition.

    The worker calls assert_acquisition_eligible() on EVERY row pulled from
    v_acquisition_queue before any cascade code runs.  The guard enforces:

      1. triage_stage must be in ACQUISITION_ELIGIBLE_STAGES
         ('accepted_for_download' only)
      2. phase4d_decision must be exactly 'ACCEPT'
      3. acquired_paper_id must be null/empty (not already downloaded)

    These checks are defense-in-depth: the view already filters for
    conditions 2 and 3, but the guard re-enforces them at the code level
    so that a future schema change or direct-table read cannot bypass them.

    If any check fails, the corresponding exception is raised immediately
    and the worker skips that row with zero file or network activity.
    """

    def assert_acquisition_eligible(self, row: dict) -> None:
        """
        Assert that `row` has cleared the full triage funnel and is eligible
        for PDF acquisition.

        Parameters
        ----------
        row : dict
            A row from article_references (or v_acquisition_queue).
            Must contain at minimum: reference_id, triage_stage,
            phase4d_decision, acquired_paper_id.

        Raises
        ------
        PrematureDownloadError  -- triage_stage is not 'accepted_for_download'
                                    OR phase4d_decision is not 'ACCEPT'
        AlreadyAcquiredError    -- acquired_paper_id is already set
        """
        reference_id     = row.get("reference_id", "<unknown>")
        triage_stage     = row.get("triage_stage", "")
        phase4d_decision = row.get("phase4d_decision", "")
        acquired_paper_id = row.get("acquired_paper_id") or ""

        # Check 1: must have passed through Phase 4D to accepted_for_download
        if triage_stage not in ACQUISITION_ELIGIBLE_STAGES:
            raise PrematureDownloadError(
                reference_id,
                f"triage_stage={triage_stage!r} is not in "
                f"ACQUISITION_ELIGIBLE_STAGES={sorted(ACQUISITION_ELIGIBLE_STAGES)}. "
                "The record has not completed the Phase 4 triage funnel. "
                "Acquisition is blocked until triage_stage = 'accepted_for_download'.",
            )

        # Check 2: Phase 4D decision must be ACCEPT
        if phase4d_decision not in ACQUISITION_ELIGIBLE_DECISIONS:
            raise PrematureDownloadError(
                reference_id,
                f"phase4d_decision={phase4d_decision!r}. "
                "Only 'ACCEPT' records may proceed to PDF acquisition. "
                "EDGE_CASE, REJECT, and MISSING_ABSTRACT are never eligible.",
            )

        # Check 3: not already acquired
        if acquired_paper_id:
            raise AlreadyAcquiredError(
                reference_id,
                f"acquired_paper_id={acquired_paper_id!r} is already set. "
                "This record has already been acquired; re-acquisition is blocked.",
            )

    def is_terminal(self, triage_stage: str) -> bool:
        """Return True if the given stage accepts no further processing."""
        return triage_stage in TERMINAL_STAGES

    def transition_is_valid(self, from_stage: str, to_stage: str) -> bool:
        """
        Return True if a transition from `from_stage` to `to_stage` is valid.

        Valid transitions enforce forward-only movement through the funnel.
        Backward transitions (e.g., 'pdf_acquired' -> 'abstract_pending') always
        return False.
        """
        if from_stage not in ALL_STAGES or to_stage not in ALL_STAGES:
            return False
        if from_stage in TERMINAL_STAGES:
            return False  # terminal stages have no outgoing transitions
        from_idx = FUNNEL_STAGES_ORDERED.index(from_stage)
        to_idx   = FUNNEL_STAGES_ORDERED.index(to_stage)
        return to_idx > from_idx
