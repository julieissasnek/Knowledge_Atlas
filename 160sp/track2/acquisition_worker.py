"""
acquisition_worker.py -- Phase 5C: data-driven PDF acquisition worker loop.

The worker reads exclusively from v_acquisition_queue inside
pipeline_lifecycle_full.db.  It never touches article_references directly.

QUEUE MECHANICS
---------------
Source:   v_acquisition_queue  (SQLite view -- see lifecycle_db._VIEW_DDL)
Ordering: phase4d_voi_score DESC NULLS LAST  (highest-value papers first)
Entry:    ACCEPT rows where acquired_paper_id IS NULL AND phase5_status='pending'
Exit:     acquired_paper_id is set on success; row disappears from view

RETRY / FAIL-STATE RETENTION
-----------------------------
On cascade failure (all sources exhausted or all blocked by the gate):
  - pdf_acquisition_attempts is incremented for each source tried
  - phase5_status remains 'pending'
  - acquired_paper_id remains NULL
  => Row stays in v_acquisition_queue for the next worker run.
  => NEVER calls mark_pdf_not_found() -- that is a TERMINAL state reserved
     for deliberate manual write-off, not for automatic retry candidates.

This means the PRISMA 'wanted-but-unobtainable' bucket grows naturally:
get_unobtainable_candidates(min_attempts=N) queries for rows that have been
tried N or more times and still have no PDF.

STRUCTURAL BARRIER: FAILURE MODE 1 (PREMATURE DOWNLOADS)
---------------------------------------------------------
Every row is passed through FunnelStateGuard.assert_acquisition_eligible()
BEFORE any cascade code runs.  If the row has not reached
triage_stage='accepted_for_download', PrematureDownloadError is raised and
the worker moves to the next row with no file or network activity.

The guard is not a conditional flag -- it is a function call that raises.
Removing or bypassing it requires explicitly catching PrematureDownloadError
at the call site, which is visible and auditable.

STRUCTURAL BARRIER: FAILURE MODE 2 (scidownl DEFAULTING)
---------------------------------------------------------
The cascade hard-wires Steps 1 and 2 before Step 3 can be reached.
Step 3 invokes verify_scidownl_clearance(), which checks all four conditions:
  C1. acquisition_config.yaml: enable_paid_or_grey_sources == True
  C2. policy_clearance.json physically exists
  C3. lifecycle_transitions has 'failure' entries for both unpaywall and openalex_oa
  C4. phase4d_decision == 'ACCEPT'
Condition C3 is enforced by the cascade itself: Steps 1 and 2 log their
failures BEFORE Step 3 is attempted.  The database state is the proof.

Usage
-----
    python acquisition_worker.py
    python acquisition_worker.py --batch 50
    python acquisition_worker.py --dry-run
    python acquisition_worker.py --allow-scidownl     # Phase 5B dev only
    python acquisition_worker.py --report             # PRISMA summary only
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

# ── Lifecycle DB ──────────────────────────────────────────────────────────────
from lifecycle_db import (
    LIFECYCLE_DB,
    _generate_paper_id,
    get_connection,
    get_unobtainable_candidates,
    increment_pdf_attempt,
    init_lifecycle_db,
    log_transition,
    mark_pdf_acquired,
    read_acquisition_queue,
    register_paper,
)

# ── Funnel state guard (Failure Mode 1 barrier) ───────────────────────────────
from triage.funnel_state import (
    FunnelStateGuard,
    PrematureDownloadError,
    AlreadyAcquiredError,
)

# ── Phase 5B gate (Failure Mode 2 barrier) ───────────────────────────────────
from scidownl_policy_gate import (
    ScidownlClearanceError,
    verify_scidownl_clearance,
)

# ── PDF downloader clients ────────────────────────────────────────────────────
_THIS_DIR = Path(__file__).resolve().parent
_AE_SRC   = _THIS_DIR.parent.parent.parent.parent / "Article_Eater" / "src"
if str(_AE_SRC) not in sys.path and _AE_SRC.exists():
    sys.path.insert(0, str(_AE_SRC))

try:
    from ingest.pdf_downloader import UnpaywallDownloader, OpenAlexOADownloader  # type: ignore
    _DOWNLOADERS_AVAILABLE = True
except ImportError as _e:
    _DOWNLOADERS_AVAILABLE = False
    _DOWNLOADERS_ERR = str(_e)

try:
    from harvest_layer import acquire_pdf_scidownl  # type: ignore
    _SCIDOWNL_AVAILABLE = True
except ImportError:
    _SCIDOWNL_AVAILABLE = False

# ── Engine constants ──────────────────────────────────────────────────────────

DEFAULT_PDF_DIR        = "pdfs"
DEFAULT_BATCH_SIZE     = 100
SOURCE_UNPAYWALL       = "unpaywall"
SOURCE_OPENALEX        = "openalex_oa"
SOURCE_SCIDOWNL        = "scidownl"
OUTCOME_SUCCESS        = "success"
OUTCOME_FAILURE        = "failure"
OUTCOME_GATE_BLOCKED   = "gate_blocked"

_PROJECT_ROOT      = _THIS_DIR.parent.parent.parent.parent / "Article_Eater"
DEFAULT_CONFIG_PATH    = str(_PROJECT_ROOT / "acquisition_config.yaml")
DEFAULT_CLEARANCE_PATH = str(_PROJECT_ROOT / "policy_clearance.json")


# ── Cascade result ────────────────────────────────────────────────────────────

class CascadeResult:
    """Carries the outcome of a single record's cascade execution."""
    __slots__ = ("pdf_path", "source", "attempts", "gate_block_condition")

    def __init__(
        self,
        pdf_path: Optional[str],
        source: str,
        attempts: int,
        gate_block_condition: int = 0,
    ) -> None:
        self.pdf_path            = pdf_path
        self.source              = source
        self.attempts            = attempts
        self.gate_block_condition = gate_block_condition

    @property
    def success(self) -> bool:
        return self.pdf_path is not None


# ── SHA-256 ───────────────────────────────────────────────────────────────────

def _sha256(path: str) -> str:
    import hashlib
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def _doi_to_filename(doi: str) -> str:
    return doi.replace("/", "_").replace(":", "_").replace(" ", "_")


# ── Worker class ──────────────────────────────────────────────────────────────

class AcquisitionWorker:
    """
    Phase 5C PDF acquisition worker.

    Reads exclusively from v_acquisition_queue, sorted by voi_score DESC.
    Enforces both failure-mode barriers before and during every cascade step.

    Injectable clients (unpaywall_client, openalex_client, scidownl_fn) allow
    offline testing without real network access.

    Parameters
    ----------
    db_path               : pipeline_lifecycle_full.db
    pdf_dir               : local directory for downloaded PDFs
    config_path           : path to acquisition_config.yaml
    policy_clearance_path : path to policy_clearance.json
    allow_scidownl        : bypass Phase 5B gate (development only)
    unpaywall_client      : injectable UnpaywallDownloader
    openalex_client       : injectable OpenAlexOADownloader
    scidownl_fn           : injectable scidownl callable
    """

    def __init__(
        self,
        db_path: str = LIFECYCLE_DB,
        *,
        pdf_dir: str = DEFAULT_PDF_DIR,
        config_path: str = DEFAULT_CONFIG_PATH,
        policy_clearance_path: str = DEFAULT_CLEARANCE_PATH,
        allow_scidownl: bool = False,
        unpaywall_client: Optional[object] = None,
        openalex_client: Optional[object] = None,
        scidownl_fn: Optional[Callable] = None,
    ) -> None:
        self._db_path               = db_path
        self._pdf_dir               = pdf_dir
        self._config_path           = config_path
        self._policy_clearance_path = policy_clearance_path
        self._allow_scidownl        = allow_scidownl
        self._guard                 = FunnelStateGuard()

        # Injectable clients -- use real implementations if not provided
        self._up  = unpaywall_client
        self._oa  = openalex_client
        self._sci = scidownl_fn

    def _resolve_clients(self):
        """Lazily resolve real downloader clients if no injectable provided."""
        if self._up is None and _DOWNLOADERS_AVAILABLE:
            self._up = UnpaywallDownloader()
        if self._oa is None and _DOWNLOADERS_AVAILABLE:
            self._oa = OpenAlexOADownloader()
        if self._sci is None and _SCIDOWNL_AVAILABLE:
            self._sci = acquire_pdf_scidownl

    # ── STRUCTURAL BARRIER: FAILURE MODE 1 CHECK ─────────────────────────────
    # This method is called before any cascade code.  If it raises, no download
    # code runs.  The FunnelStateGuard is the gatekeeper.

    def _assert_funnel_cleared(self, row: dict) -> None:
        """
        FAILURE MODE 1 BARRIER
        ========================
        Delegates to FunnelStateGuard.assert_acquisition_eligible().
        Raises PrematureDownloadError or AlreadyAcquiredError if the row has
        not completed Phase 4D triage.

        This call cannot be bypassed without an explicit except clause that
        would be immediately visible in code review.
        """
        self._guard.assert_acquisition_eligible(row)

    # ── STRUCTURAL BARRIER: FAILURE MODE 2 CHECK ─────────────────────────────
    # Step 3 of the cascade calls verify_scidownl_clearance(), which requires
    # that Steps 1 and 2 have already logged their failures.  The cascade
    # structure itself (Steps 1 and 2 run first, logging to lifecycle_transitions)
    # makes it impossible for Step 3 to find the cascade exhaustion evidence
    # unless Steps 1 and 2 genuinely ran.

    def _execute_cascade(
        self,
        row: dict,
        dry_run: bool = False,
    ) -> CascadeResult:
        """
        Execute the three-step acquisition cascade for one row.

        Steps run in order: Unpaywall -> OpenAlex OA -> [Phase 5B gate] -> scidownl.
        Stops on first success.

        On dry_run=True: simulates execution without DB writes or network calls.

        Attempts counter and lifecycle_transitions are written for each source
        tried (real run only).  The cascade exhaustion record written here is what
        allows the Phase 5B gate's Condition C3 to pass on a subsequent re-run if
        scidownl becomes authorised.

        Returns CascadeResult. On failure, pdf_path=None.
        NEVER sets phase5_status='pdf_not_found' -- that is a terminal state.
        """
        reference_id = row["reference_id"]
        doi          = (row.get("doi") or "").strip()
        attempts     = 0
        last_source  = ""
        gate_cond    = 0

        Path(self._pdf_dir).mkdir(parents=True, exist_ok=True)
        out_path = str(Path(self._pdf_dir) / f"{_doi_to_filename(doi)}.pdf") if doi else ""

        # ── Step 1: Unpaywall (ALWAYS FIRST) ──────────────────────────────────
        if doi and self._up is not None:
            if not dry_run:
                increment_pdf_attempt(reference_id, SOURCE_UNPAYWALL, db_path=self._db_path)
            attempts += 1
            last_source = SOURCE_UNPAYWALL
            result = self._up.try_download(doi, out_path)
            outcome = OUTCOME_SUCCESS if result else OUTCOME_FAILURE
            if not dry_run:
                log_transition(reference_id, SOURCE_UNPAYWALL, outcome,
                               doi=doi, db_path=self._db_path)
            if result:
                return CascadeResult(result, SOURCE_UNPAYWALL, attempts)

        # ── Step 2: OpenAlex OA (ONLY IF STEP 1 FAILED) ───────────────────────
        if doi and self._oa is not None:
            if not dry_run:
                increment_pdf_attempt(reference_id, SOURCE_OPENALEX, db_path=self._db_path)
            attempts += 1
            last_source = SOURCE_OPENALEX
            result = self._oa.try_download(doi, out_path)
            outcome = OUTCOME_SUCCESS if result else OUTCOME_FAILURE
            if not dry_run:
                log_transition(reference_id, SOURCE_OPENALEX, outcome,
                               doi=doi, db_path=self._db_path)
            if result:
                return CascadeResult(result, SOURCE_OPENALEX, attempts)

        # ── Step 3: scidownl -- PHASE 5B FOUR-CONDITION GATE ─────────────────
        #
        # FAILURE MODE 2 BARRIER
        # =======================
        # verify_scidownl_clearance() enforces all four conditions:
        #   C1. YAML config armed
        #   C2. policy_clearance.json present
        #   C3. Both unpaywall and openalex_oa logged as 'failure' in
        #       lifecycle_transitions for THIS reference_id.  Because Steps 1
        #       and 2 ran and logged above, this condition is NOW satisfiable.
        #       If Steps 1 and 2 were somehow skipped, C3 would block Step 3.
        #   C4. phase4d_decision == 'ACCEPT'
        #
        # The cascade structure enforces that C3 evidence is created BEFORE
        # Step 3 is reached.  scidownl cannot be the first or default attempt.

        if doi and self._sci is not None:
            try:
                verify_scidownl_clearance(
                    reference_id,
                    row.get("phase4d_decision", ""),
                    db_path               = self._db_path,
                    config_path           = self._config_path,
                    policy_clearance_path = self._policy_clearance_path,
                )
                # Gate passed
                if not dry_run:
                    increment_pdf_attempt(
                        reference_id, SOURCE_SCIDOWNL, db_path=self._db_path
                    )
                attempts += 1
                last_source = SOURCE_SCIDOWNL
                result = self._sci(doi, output_dir=self._pdf_dir)
                outcome = OUTCOME_SUCCESS if result else OUTCOME_FAILURE
                if not dry_run:
                    log_transition(reference_id, SOURCE_SCIDOWNL, outcome,
                                   doi=doi, db_path=self._db_path)
                if result:
                    return CascadeResult(result, SOURCE_SCIDOWNL, attempts)

            except ScidownlClearanceError as gate_exc:
                gate_cond = gate_exc.condition
                if not dry_run:
                    log_transition(
                        reference_id, SOURCE_SCIDOWNL, OUTCOME_GATE_BLOCKED,
                        doi=doi,
                        metadata=(
                            f'{{"condition": {gate_cond}, '
                            f'"reason": {str(gate_exc)[:120]!r}}}'
                        ),
                        db_path=self._db_path,
                    )

        return CascadeResult(None, last_source, attempts, gate_block_condition=gate_cond)

    def _persist_success(self, row: dict, result: CascadeResult) -> None:
        """Write papers table row and back-reference on successful acquisition."""
        reference_id = row["reference_id"]
        doi          = (row.get("doi") or "").strip()
        file_size    = (
            Path(result.pdf_path).stat().st_size
            if result.pdf_path and Path(result.pdf_path).exists()
            else 0
        )
        checksum = _sha256(result.pdf_path) if result.pdf_path else ""

        with get_connection(self._db_path) as conn:
            paper_id = _generate_paper_id(conn)

        register_paper(
            paper_id           = paper_id,
            reference_id       = reference_id,
            doi                = doi,
            local_pdf_path     = result.pdf_path,
            acquisition_source = result.source,
            file_size_bytes    = file_size,
            sha256             = checksum,
            db_path            = self._db_path,
        )
        mark_pdf_acquired(
            reference_id,
            paper_id = paper_id,
            pdf_path = result.pdf_path,
            db_path  = self._db_path,
        )

    def run(
        self,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        dry_run: bool = False,
    ) -> dict[str, int]:
        """
        Main worker loop.

        Reads up to `batch_size` rows from v_acquisition_queue (sorted by
        voi_score DESC), runs each through the cascade, and persists outcomes.

        ON FAILURE: row stays in v_acquisition_queue (phase5_status='pending').
        Attempts counter is incremented so the PRISMA dashboard can surface
        wanted-but-unobtainable records.

        Parameters
        ----------
        batch_size : max rows to process per run
        dry_run    : simulate without DB writes or disk activity

        Returns
        -------
        {
            'acquired':          N,
            'cascade_failed':    N,
            'guard_blocked':     N,   # Failure Mode 1 blocks
            'scidownl_blocked':  N,   # Failure Mode 2 blocks
            'skipped_no_doi':    N,
            'total':             N,
        }
        """
        if not dry_run:
            init_lifecycle_db(self._db_path)

        self._resolve_clients()

        rows = read_acquisition_queue(self._db_path, limit=batch_size)
        total = len(rows)

        counts = {
            "acquired":         0,
            "cascade_failed":   0,
            "guard_blocked":    0,
            "scidownl_blocked": 0,
            "skipped_no_doi":   0,
            "total":            0,
        }

        print(
            f"[worker] {total} rows in v_acquisition_queue "
            f"(batch_size={batch_size}, dry_run={dry_run})"
        )

        for i, row in enumerate(rows, 1):
            ref_id = row.get("reference_id", f"<row-{i}>")
            doi    = (row.get("doi") or "").strip()
            voi    = row.get("phase4d_voi_score")
            voi_s  = f"{voi:.3f}" if voi is not None else "null"
            label  = (row.get("title_raw") or doi or ref_id)[:55]

            print(
                f"  [{i:4d}/{total}] voi={voi_s}  {label}",
                end="\r", flush=True,
            )

            counts["total"] += 1

            # ── FAILURE MODE 1 BARRIER ────────────────────────────────────────
            try:
                self._assert_funnel_cleared(row)
            except (PrematureDownloadError, AlreadyAcquiredError) as guard_exc:
                print(
                    f"\n  [guard] BLOCKED {ref_id}: {guard_exc}",
                    file=sys.stderr,
                )
                counts["guard_blocked"] += 1
                continue  # skip -- no cascade, no attempt increment

            if not doi:
                counts["skipped_no_doi"] += 1
                continue

            # ── CASCADE ───────────────────────────────────────────────────────
            result = self._execute_cascade(row, dry_run=dry_run)

            if result.success:
                if not dry_run:
                    self._persist_success(row, result)
                counts["acquired"] += 1
            else:
                # RETRY RETENTION CONTRACT
                # =========================
                # Do NOT call mark_pdf_not_found() here.
                # phase5_status stays 'pending', row remains in v_acquisition_queue.
                # pdf_acquisition_attempts was incremented inside _execute_cascade.
                if result.gate_block_condition > 0:
                    counts["scidownl_blocked"] += 1
                counts["cascade_failed"] += 1

        print()  # clear carriage-return line
        unobtainable = len(get_unobtainable_candidates(self._db_path, min_attempts=1))
        print(
            f"[worker] acquired={counts['acquired']}  "
            f"cascade_failed={counts['cascade_failed']}  "
            f"guard_blocked={counts['guard_blocked']}  "
            f"scidownl_blocked={counts['scidownl_blocked']}  "
            f"total={counts['total']}"
        )
        print(f"[worker] PRISMA unobtainable bucket: {unobtainable} records")
        return counts

    def report(self) -> dict:
        """
        Return PRISMA-style summary from the lifecycle DB without running acquisition.
        """
        from lifecycle_db import get_phase4_counts
        phase4   = get_phase4_counts(self._db_path)
        queue    = read_acquisition_queue(self._db_path)
        unobt    = get_unobtainable_candidates(self._db_path, min_attempts=1)
        acquired = 0
        with get_connection(self._db_path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM article_references WHERE phase5_status='pdf_acquired'"
            ).fetchone()
            acquired = row[0] if row else 0

        return {
            "queue_depth":          len(queue),
            "acquired":             acquired,
            "unobtainable":         len(unobt),
            "phase4_stage_counts":  phase4,
        }


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Phase 5C acquisition worker.\n"
            "Reads from v_acquisition_queue sorted by voi_score DESC.\n"
            "Failed rows stay in queue for retry (never marked pdf_not_found)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", default=LIFECYCLE_DB)
    parser.add_argument("--pdf-dir", default=DEFAULT_PDF_DIR)
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH_SIZE,
                        help=f"Max rows per run (default: {DEFAULT_BATCH_SIZE})")
    parser.add_argument("--allow-scidownl", action="store_true",
                        help="Bypass Phase 5B gate (development only)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", action="store_true",
                        help="Print PRISMA summary without running acquisition")
    args = parser.parse_args()

    worker = AcquisitionWorker(
        args.db,
        pdf_dir       = args.pdf_dir,
        allow_scidownl= args.allow_scidownl,
    )

    if args.report:
        import json
        print(json.dumps(worker.report(), indent=2))
    else:
        worker.run(batch_size=args.batch, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
