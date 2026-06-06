"""
abstract_collector_4c.py -- Phase 4C abstract collection orchestrator.

Reads rows with triage_stage = 'abstract_pending' from pipeline_lifecycle_full.db,
runs a four-source fallback chain to collect a validated full abstract, and
writes the result back via lifecycle_db.update_triage_stage().

Fallback chain (short-circuits on first validated hit):
  1. Semantic Scholar  by DOI  (if doi present)
  2. Semantic Scholar  by title
  3. CrossRef          by DOI  (if doi present)
  4. CrossRef          by title
  5. PubMed            by DOI  (if doi present)
  6. PubMed            by title
  7. OpenAlex          by DOI  (if doi present)
  8. OpenAlex          by title

A result is accepted only if _is_valid_abstract() passes:
  - len(text) > 150 characters
  - does not end with a truncation marker (...  or [...]  or Ellipsis)
  - not a snippet echo: Levenshtein-like similarity to known snippet < 0.85

Terminal exhaustion -> triage_stage = 'abstract_missing'.

CONTRACT CLAUSES:
  P4C-1  Bind to SemanticScholarClient / CrossRefClient / PubMedClient /
         OpenAlexHelper defined in Article_Eater/src/services/paper_fetcher.py.
  P4C-2  Semantic Scholar rate-limited to <= 20 req/min (enforced by client).
  P4C-3  Abstract > 150 chars; no trailing truncation marker.
  P4C-4  Title similarity >= 0.90 enforced by paper_fetcher clients.
  P4C-5  Write enriched records to pipeline_lifecycle_full.db via
         lifecycle_db.update_triage_stage().
  P4C-6  MISSING_ABSTRACT on all-source exhaustion; zero silent drops.
  P4C-7  Hit-rate >= 0.70 on records that have a valid DOI (logged, not enforced).

Usage:
    python abstract_collector_4c.py                    # run against default DB
    python abstract_collector_4c.py --db /path/to.db
    python abstract_collector_4c.py --dry-run          # report without writing
    python abstract_collector_4c.py --delay 1.0        # seconds between sources
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Optional

# ── Paper-fetcher clients (Phase 4C contract P4C-1) ──────────────────────────
# Resolve the Article_Eater package.  Adds the src directory to sys.path
# when running the script directly so the import works without installing.
import os as _os

_THIS_DIR = Path(__file__).resolve().parent  # track2/
_AE_SRC   = _THIS_DIR.parent.parent.parent.parent / "Article_Eater" / "src"
if str(_AE_SRC) not in sys.path and _AE_SRC.exists():
    sys.path.insert(0, str(_AE_SRC))

try:
    from services.paper_fetcher import (  # type: ignore
        SemanticScholarClient,
        CrossRefClient,
        PubMedClient,
        OpenAlexHelper,
    )
    _FETCHERS_AVAILABLE = True
except ImportError as _e:
    _FETCHERS_AVAILABLE = False
    _FETCHERS_IMPORT_ERR = str(_e)

# ── Lifecycle DB ──────────────────────────────────────────────────────────────
from lifecycle_db import (
    LIFECYCLE_DB,
    TRIAGE_STAGE_ABSTRACT_COLLECTED,
    TRIAGE_STAGE_ABSTRACT_MISSING,
    get_pending_abstract_collection,
    log_transition,
    update_triage_stage,
)

# ── Constants ─────────────────────────────────────────────────────────────────

ABSTRACT_MIN_CHARS      = 150    # P4C-3: minimum character length
SNIPPET_SIM_CEILING     = 0.85   # reject if similarity to harvest snippet >= this
HIT_RATE_FLOOR          = 0.70   # P4C-7: warn if hit rate on DOI records falls below

_TRUNCATION_MARKERS = ("...", "[...]", "…", "... [truncated]")


# ── Abstract validation (P4C-3) ───────────────────────────────────────────────

def _is_valid_abstract(text: str, snippet: str = "") -> bool:
    """
    Return True iff the candidate text is a genuine full abstract.

    Rules:
      1. Must be longer than ABSTRACT_MIN_CHARS characters (after strip).
      2. Must not end with a truncation marker.
      3. Must not be a near-duplicate of the harvest snippet (snippet echo).
         Ratio checked with SequenceMatcher; rejection threshold SNIPPET_SIM_CEILING.
    """
    if not text:
        return False
    text = text.strip()
    if len(text) <= ABSTRACT_MIN_CHARS:
        return False
    # Truncation marker check
    for marker in _TRUNCATION_MARKERS:
        if text.endswith(marker):
            return False
    # Snippet-echo check (P4C-3 anti-echo)
    if snippet:
        from difflib import SequenceMatcher
        ratio = SequenceMatcher(None, text.lower(), snippet.lower()).ratio()
        if ratio >= SNIPPET_SIM_CEILING:
            return False
    return True


# ── Single-record fallback chain ──────────────────────────────────────────────

def _fetch_abstract(
    doi: str,
    title: str,
    *,
    ss: "SemanticScholarClient",
    cr: "CrossRefClient",
    pm: "PubMedClient",
    oa: "OpenAlexHelper",
    snippet: str = "",
    delay: float = 0.3,
) -> tuple[str, str]:
    """
    Run the four-source fallback chain.

    Returns (abstract_text, source_name) where source_name is one of:
      'semantic_scholar', 'crossref', 'pubmed', 'openalex', or '' on exhaustion.

    Short-circuits immediately on the first result that passes _is_valid_abstract().
    """

    def _try(result: Optional[dict]) -> tuple[str, str]:
        if result is None:
            return "", ""
        abstract = (result.get("abstract") or "").strip()
        source   = result.get("source", "")
        if _is_valid_abstract(abstract, snippet):
            return abstract, source
        return "", ""

    # Source 1: Semantic Scholar
    if doi:
        abstract, source = _try(ss.by_doi(doi))
        if abstract:
            return abstract, source
    abstract, source = _try(ss.by_title(title))
    if abstract:
        return abstract, source
    time.sleep(delay)

    # Source 2: CrossRef
    if doi:
        abstract, source = _try(cr.by_doi(doi))
        if abstract:
            return abstract, source
    abstract, source = _try(cr.by_title(title))
    if abstract:
        return abstract, source
    time.sleep(delay)

    # Source 3: PubMed
    if doi:
        abstract, source = _try(pm.by_doi(doi))
        if abstract:
            return abstract, source
    abstract, source = _try(pm.by_title(title))
    if abstract:
        return abstract, source
    time.sleep(delay)

    # Source 4: OpenAlex
    if doi:
        abstract, source = _try(oa.by_doi(doi))
        if abstract:
            return abstract, source
    abstract, source = _try(oa.by_title(title))
    if abstract:
        return abstract, source

    # All sources exhausted — P4C-6: signal MISSING, never silent-drop
    return "", ""


# ── Main batch processor ───────────────────────────────────────────────────────

def run_phase4c_abstract_collection(
    db_path: str = LIFECYCLE_DB,
    *,
    delay: float = 0.3,
    dry_run: bool = False,
    ss: Optional["SemanticScholarClient"] = None,
    cr: Optional["CrossRefClient"] = None,
    pm: Optional["PubMedClient"] = None,
    oa: Optional["OpenAlexHelper"] = None,
) -> dict[str, int]:
    """
    Process all 'abstract_pending' rows in the lifecycle DB.

    Parameters
    ----------
    db_path   : path to pipeline_lifecycle_full.db
    delay     : seconds to sleep between source groups (polite pacing)
    dry_run   : if True, report what would happen without writing
    ss/cr/pm/oa: injectable client instances (for testing)

    Returns
    -------
    {
      'abstract_collected': N,
      'abstract_missing':   N,
      'skipped':            N,   # dry_run rows not written
    }
    """
    if not _FETCHERS_AVAILABLE:
        print(
            f"[4c] paper_fetcher not importable: {_FETCHERS_IMPORT_ERR}\n"
            "     Ensure Article_Eater/src is on PYTHONPATH.",
            file=sys.stderr,
        )
        return {TRIAGE_STAGE_ABSTRACT_COLLECTED: 0, TRIAGE_STAGE_ABSTRACT_MISSING: 0, "skipped": 0}

    # Instantiate shared clients once for the whole batch
    _ss = ss or SemanticScholarClient()
    _cr = cr or CrossRefClient()
    _pm = pm or PubMedClient()
    _oa = oa or OpenAlexHelper()

    pending = get_pending_abstract_collection(db_path)
    total   = len(pending)

    counts = {
        TRIAGE_STAGE_ABSTRACT_COLLECTED: 0,
        TRIAGE_STAGE_ABSTRACT_MISSING:   0,
        "skipped": 0,
    }

    doi_records   = 0   # rows that have a DOI
    doi_hits      = 0   # DOI rows that got a valid abstract

    for i, row in enumerate(pending, 1):
        ref_id  = row["reference_id"]
        doi     = (row.get("doi") or "").strip()
        title   = (row.get("title_raw") or "").strip()
        snippet = (row.get("snippet") or "").strip()

        if not title and not doi:
            # Unresolvable — mark immediately
            if not dry_run:
                update_triage_stage(
                    ref_id,
                    TRIAGE_STAGE_ABSTRACT_MISSING,
                    abstract_source="",
                    db_path=db_path,
                )
                log_transition(
                    ref_id,
                    "phase4b_abstract_collection",
                    "failure",
                    doi="",
                    metadata='{"reason": "no_title_no_doi"}',
                    db_path=db_path,
                )
            counts[TRIAGE_STAGE_ABSTRACT_MISSING] += 1
            continue

        print(
            f"  [4c {i}/{total}] {(title or doi)[:68]}...",
            end="\r",
            flush=True,
        )

        if doi:
            doi_records += 1

        abstract, source = _fetch_abstract(
            doi, title,
            ss=_ss, cr=_cr, pm=_pm, oa=_oa,
            snippet=snippet,
            delay=delay,
        )

        if abstract:
            stage = TRIAGE_STAGE_ABSTRACT_COLLECTED
            counts[TRIAGE_STAGE_ABSTRACT_COLLECTED] += 1
            if doi:
                doi_hits += 1
        else:
            stage = TRIAGE_STAGE_ABSTRACT_MISSING
            counts[TRIAGE_STAGE_ABSTRACT_MISSING] += 1

        if dry_run:
            counts["skipped"] += 1
        else:
            update_triage_stage(
                ref_id,
                stage,
                abstract=abstract or None,
                abstract_source=source or None,
                db_path=db_path,
            )
            # Atomic audit row in lifecycle_transitions (C5 state-transition contract)
            log_transition(
                ref_id,
                "phase4b_abstract_collection",
                "success" if abstract else "failure",
                doi=doi,
                metadata=(
                    f'{{"abstract_source": "{source}", '
                    f'"abstract_length": {len(abstract)}}}'
                    if abstract else
                    f'{{"abstract_source": "none", "abstract_length": 0}}'
                ),
                db_path=db_path,
            )

    # ── P4C-7: hit-rate reporting ─────────────────────────────────────────────
    print()  # clear carriage-return line
    if not dry_run:
        collected = counts[TRIAGE_STAGE_ABSTRACT_COLLECTED]
        missing   = counts[TRIAGE_STAGE_ABSTRACT_MISSING]
        print(
            f"[4c] abstract_collected={collected}  "
            f"abstract_missing={missing}  "
            f"total={total}"
        )
        if doi_records > 0:
            hit_rate = doi_hits / doi_records
            flag = "  [WARNING: below 0.70 floor]" if hit_rate < HIT_RATE_FLOOR else ""
            print(f"[4c] DOI-record hit rate: {doi_hits}/{doi_records} = {hit_rate:.2%}{flag}")
    else:
        print(
            f"[4c] dry-run: {total} pending rows — "
            f"would run fallback chain on each"
        )

    return counts


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Phase 4C abstract collection — fetch validated full abstracts "
            "for all abstract_pending rows via SS -> CrossRef -> PubMed -> OpenAlex."
        )
    )
    parser.add_argument(
        "--db", default=LIFECYCLE_DB,
        help=f"Path to pipeline_lifecycle_full.db (default: {LIFECYCLE_DB})"
    )
    parser.add_argument(
        "--delay", type=float, default=0.3,
        help="Seconds to sleep between source groups per record (default: 0.3)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report what would happen without writing to DB"
    )
    args = parser.parse_args()

    run_phase4c_abstract_collection(
        args.db,
        delay=args.delay,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
