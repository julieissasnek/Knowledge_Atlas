"""
abstract_triage.py — CLI wrapper for the Phase 4D abstract triage engine.

Reads papers from a JSON file (output of abstract_collector.py), runs each
abstract through the domain-relevance classifier and VOI scorer, and writes
triage decisions to triage_results.json.

This is the submission-facing entry point for Track 2, Task 3, Phase 4D.
The core triage logic lives in abstract_triage_4d.py (classifier, VOI, DB
persistence) and is imported here without modification.

Usage
-----
    python abstract_triage.py
    python abstract_triage.py --papers papers_with_abstracts.json --out triage_results.json
    python abstract_triage.py --db /path/to/pipeline_lifecycle_full.db
    python abstract_triage.py --dry-run          # classify but do not write to DB

Decision rules
--------------
  1. Missing abstract     -> MISSING_ABSTRACT  (no classifier call)
  2. topic_confidence < 0.65 -> REJECT         (off-topic)
  3. voi_score >= 0.70    -> ACCEPT
  4. voi_score >= 0.50    -> EDGE_CASE
  5. voi_score <  0.50    -> REJECT            (low information value)

Output fields (per paper in triage_results.json)
-------------------------------------------------
  paper_id            -- reference_id or DOI
  title               -- paper title
  doi                 -- normalized DOI
  gap_id              -- originating research gap
  abstract_source     -- which API provided the abstract
  triage_decision     -- ACCEPT | EDGE_CASE | REJECT | MISSING_ABSTRACT
  triage_reason       -- human-readable explanation
  topic_confidence    -- float 0-1 from domain classifier
  voi_score           -- float 0-1 from score_voi()
  timestamp           -- ISO 8601 decision time
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── Ensure track2/ and Article_Eater/src are on path ────────────────────────
_HERE   = Path(__file__).resolve().parent
_AE_SRC = _HERE.parent.parent.parent.parent / "Article_Eater" / "src"
sys.path.insert(0, str(_HERE))
if _AE_SRC.exists():
    sys.path.insert(0, str(_AE_SRC))

from lifecycle_db import LIFECYCLE_DB, get_abstract_collected_rows, get_connection
from abstract_triage_4d import (
    triage_one,
    DECISION_ACCEPT,
    DECISION_EDGE_CASE,
    DECISION_REJECT,
    DECISION_MISSING_ABSTRACT,
)

DEFAULT_IN_FILE  = "papers_with_abstracts.json"
DEFAULT_OUT_FILE = "triage_results.json"


def _papers_from_json(path: str) -> list[dict]:
    """Load papers from a JSON file produced by abstract_collector.py."""
    p = Path(path)
    if not p.exists():
        print(f"[abstract_triage] Input file not found: {path}", file=sys.stderr)
        return []
    return json.loads(p.read_text(encoding="utf-8"))


def _papers_from_db(db_path: str) -> list[dict]:
    """Load abstract_collected rows from pipeline_lifecycle_full.db."""
    return get_abstract_collected_rows(db_path)


def triage_papers(
    papers: list[dict],
    *,
    db_path: str = LIFECYCLE_DB,
    dry_run: bool = False,
) -> list[dict]:
    """
    Classify each paper and return a list of triage result dicts.
    If dry_run=False, writes decisions to pipeline_lifecycle_full.db.
    """
    from abstract_triage_4d import run_phase4d_triage, _persist_record

    results: list[dict] = []
    for paper in papers:
        record = triage_one(paper)
        result = {
            "paper_id":         record.paper_id,
            "title":            paper.get("title_raw") or paper.get("title") or "",
            "doi":              paper.get("doi", ""),
            "gap_id":           paper.get("discovered_query") or paper.get("gap_id", ""),
            "abstract_source":  paper.get("abstract_source", ""),
            "triage_decision":  record.triage_decision,
            "triage_reason":    record.triage_reason,
            "topic_confidence": record.topic_confidence,
            "voi_score":        record.voi_score,
            "timestamp":        record.timestamp,
        }
        results.append(result)
        if not dry_run:
            ref_id = paper.get("reference_id") or record.paper_id
            try:
                _persist_record(ref_id, record, db_path)
            except Exception:
                pass  # reference_id may not be in lifecycle DB when reading from JSON
    return results


def export_triage_json(
    in_file: str = DEFAULT_IN_FILE,
    out_file: str = DEFAULT_OUT_FILE,
    *,
    db_path: str = LIFECYCLE_DB,
    dry_run: bool = False,
    from_db: bool = False,
) -> list[dict]:
    """
    Main export function: load papers, triage, write JSON.
    Returns the list of triage result dicts.
    """
    if from_db:
        papers = _papers_from_db(db_path)
        if not papers:
            # Fall back to JSON file if DB has no abstract_collected rows
            papers = _papers_from_json(in_file)
    else:
        papers = _papers_from_json(in_file)
        if not papers:
            papers = _papers_from_db(db_path)

    print(f"[abstract_triage] {len(papers)} papers to classify")

    if not papers:
        print("[abstract_triage] No papers found — nothing to triage.", file=sys.stderr)
        return []

    results = triage_papers(papers, db_path=db_path, dry_run=dry_run)

    counts = {d: sum(1 for r in results if r["triage_decision"] == d)
              for d in [DECISION_ACCEPT, DECISION_EDGE_CASE,
                        DECISION_REJECT, DECISION_MISSING_ABSTRACT]}
    print(f"[abstract_triage] Decisions: {counts}")

    out = {
        "generated_at":   datetime.now(timezone.utc).isoformat(),
        "total":          len(results),
        "counts":         counts,
        "papers":         results,
    }
    Path(out_file).write_text(
        json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"[abstract_triage] Wrote {len(results)} decisions to {out_file}")
    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Abstract triage engine — classifies papers as ACCEPT / EDGE_CASE / "
            "REJECT / MISSING_ABSTRACT using the domain classifier and VOI scorer."
        )
    )
    parser.add_argument("--papers",  "-p", default=DEFAULT_IN_FILE,
                        help=f"Input JSON (default: {DEFAULT_IN_FILE})")
    parser.add_argument("--out",     "-o", default=DEFAULT_OUT_FILE,
                        help=f"Output JSON (default: {DEFAULT_OUT_FILE})")
    parser.add_argument("--db",      default=LIFECYCLE_DB,
                        help="pipeline_lifecycle_full.db path")
    parser.add_argument("--from-db", action="store_true",
                        help="Read papers from DB instead of JSON file")
    parser.add_argument("--dry-run", action="store_true",
                        help="Classify but do not write decisions to DB")
    args = parser.parse_args()

    export_triage_json(
        in_file  = args.papers,
        out_file = args.out,
        db_path  = args.db,
        dry_run  = args.dry_run,
        from_db  = args.from_db,
    )


if __name__ == "__main__":
    main()
