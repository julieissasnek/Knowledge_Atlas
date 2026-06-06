"""
abstract_triage_4d.py -- Phase 4D: Abstract-level triage.

Reads rows with triage_stage = 'abstract_collected' from
pipeline_lifecycle_full.db, classifies each abstract for domain relevance,
scores its Value-of-Information, and persists the decision.

Decision pipeline
-----------------
  1. MISSING_ABSTRACT guard
     Abstract is null / empty / whitespace-only.
     -> decision = MISSING_ABSTRACT; no classifier or VOI call.

  2. Topic classification
     Run AbstractRelevanceClassifier (or atlas_shared if available) on
     title + abstract.  Extract topic_confidence in [0.0, 1.0].
     If topic_confidence < TOPIC_THRESHOLD (0.65):
     -> decision = REJECT (off-topic).

  3. VOI scoring
     Call score_voi() from cmr/voi_scoring.py.
     voi_score >= VOI_ACCEPT  (0.70) -> ACCEPT
     voi_score >= VOI_EDGE    (0.50) -> EDGE_CASE
     voi_score <  VOI_EDGE           -> REJECT

Persistence
-----------
  ACCEPT      -> article_references.triage_stage = 'accepted_for_download'
                 phase4d columns written on article_references
  EDGE_CASE   -> article_references.triage_stage = 'edge_case_review'
                 row inserted into edge_case_review_queue (manual_review_flag=1)
  REJECT      -> article_references.triage_stage = 'rejected_at_abstract'
                 row inserted into triage_decision_log
  MISSING_ABSTRACT -> article_references.triage_stage = 'rejected_at_abstract'
                 row inserted into triage_decision_log with decision='MISSING_ABSTRACT'
                 NOT counted as a domain reject; distinguishable by decision field.

Usage
-----
  python abstract_triage_4d.py
  python abstract_triage_4d.py --db /path/to.db
  python abstract_triage_4d.py --dry-run
  python abstract_triage_4d.py --limit 50
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

# ── Lifecycle DB ──────────────────────────────────────────────────────────────
from lifecycle_db import (
    LIFECYCLE_DB,
    TRIAGE_STAGE_ABSTRACT_COLLECTED,
    TRIAGE_STAGE_ACCEPTED_FOR_DOWNLOAD,
    TRIAGE_STAGE_EDGE_CASE_REVIEW,
    TRIAGE_STAGE_REJECTED_AT_ABSTRACT,
    get_abstract_collected_rows,
    get_connection,
    init_lifecycle_db,
)

# ── Paper-fetcher path (for score_voi import) ─────────────────────────────────
_THIS_DIR = Path(__file__).resolve().parent
_AE_SRC   = _THIS_DIR.parent.parent.parent.parent / "Article_Eater" / "src"
if str(_AE_SRC) not in sys.path and _AE_SRC.exists():
    sys.path.insert(0, str(_AE_SRC))

try:
    from cmr.voi_scoring import score_voi as _default_voi_scorer  # type: ignore
    _VOI_AVAILABLE = True
except ImportError:
    _default_voi_scorer = None  # type: ignore
    _VOI_AVAILABLE = False

# ── Domain signals (shared with triage_funnel.py) ─────────────────────────────
from triage_funnel import (
    _STRONG_SIGNALS,
    _VENUE_KEYWORDS,
    _WEAK_SIGNALS,
    _build_classifier,
    _ClassificationEvidence,
)


# ── Decision constants ─────────────────────────────────────────────────────────

DECISION_ACCEPT           = "ACCEPT"
DECISION_EDGE_CASE        = "EDGE_CASE"
DECISION_REJECT           = "REJECT"
DECISION_MISSING_ABSTRACT = "MISSING_ABSTRACT"

TOPIC_THRESHOLD = 0.65   # abstract classifier minimum for on-topic
VOI_ACCEPT      = 0.70   # VOI score -> ACCEPT
VOI_EDGE        = 0.50   # VOI score -> EDGE_CASE (below = REJECT)


# ── TriageRecord output dataclass ─────────────────────────────────────────────

@dataclass
class TriageRecord:
    paper_id:          str
    triage_decision:   str
    triage_reason:     str
    topic_confidence:  Optional[float]
    voi_score:         Optional[float]
    timestamp:         str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __post_init__(self):
        assert self.triage_decision in (
            DECISION_ACCEPT, DECISION_EDGE_CASE,
            DECISION_REJECT, DECISION_MISSING_ABSTRACT,
        ), f"Invalid triage_decision: {self.triage_decision!r}"
        assert self.triage_reason, "triage_reason must be non-empty"


# ── Abstract-level classifier ─────────────────────────────────────────────────

class AbstractRelevanceClassifier:
    """
    Domain-relevance scorer operating on title + FULL ABSTRACT.

    Scores strong and weak signals separately in title vs abstract text.
    Threshold is TOPIC_THRESHOLD = 0.65 (higher than Phase 4A's 0.20 because
    we have much more text to work with here).

    Score formula:
      strong_title   * 0.25  (each, uncapped)
      strong_abstract * 0.10  (each, uncapped)
      weak_title     * 0.05  (each, uncapped)
      weak_abstract  * 0.02  (each, uncapped)
      venue_bonus    * 0.10  (once)
      Clamped to [0.0, 1.0].

    Example results for a daylight+cognition paper:
      3 strong in title + 5 strong in abstract: 0.75 + 0.50 = 1.0 (clamped)
      2 strong in title + 3 strong in abstract: 0.50 + 0.30 = 0.80
      1 strong in title + 2 strong in abstract: 0.25 + 0.20 = 0.45  REJECT
      1 strong in title + 4 strong in abstract: 0.25 + 0.40 = 0.65  PASSES
    """

    def classify(self, evidence) -> float:
        """Return domain-relevance confidence in [0.0, 1.0]."""
        title    = (getattr(evidence, "title",    "") or "").lower()
        abstract = (getattr(evidence, "abstract", "") or "").lower()
        venue    = (getattr(evidence, "venue",    "") or "").lower()

        score = 0.0
        for signal in _STRONG_SIGNALS:
            if signal in title:
                score += 0.25
            if signal in abstract:
                score += 0.10

        for signal in _WEAK_SIGNALS:
            if signal in title:
                score += 0.05
            if signal in abstract:
                score += 0.02

        for kw in _VENUE_KEYWORDS:
            if kw in venue:
                score += 0.10
                break

        return min(round(score, 4), 1.0)


def _build_abstract_classifier():
    """
    Return (classifier_callable, backend_name).

    Resolution order:
      1. atlas_shared.AdaptiveClassifierSubsystem (if installed)
         Wrapped to return float from result.article_type.confidence.
      2. AbstractRelevanceClassifier -- abstract-aware keyword scorer.
    """
    import os as _os
    ka_src = _os.environ.get("KA_ATLAS_SHARED_SRC", "")
    if ka_src and ka_src not in sys.path:
        sys.path.insert(0, ka_src)

    try:
        from atlas_shared.classifier_system import (  # type: ignore
            AdaptiveClassifierSubsystem,
            ClassificationEvidence,
        )
        _atlas = AdaptiveClassifierSubsystem()

        def _atlas_classify(paper: dict) -> float:
            ev = ClassificationEvidence(
                title    = paper.get("title_raw") or paper.get("title") or "",
                abstract = paper.get("abstract") or "",
            )
            return _atlas.classify(ev).article_type.confidence

        return _atlas_classify, "atlas_shared"
    except (ImportError, ModuleNotFoundError):
        pass

    _clf = AbstractRelevanceClassifier()

    def _local_classify(paper: dict) -> float:
        ev = _ClassificationEvidence(
            title    = paper.get("title_raw") or paper.get("title") or "",
            abstract = paper.get("abstract") or "",
            venue    = paper.get("venue") or "",
        )
        return _clf.classify(ev)

    return _local_classify, "abstract_relevance_classifier"


# Module-level defaults (override via triage_one(classifier=...) for tests)
_DEFAULT_CLASSIFIER, CLASSIFIER_BACKEND = _build_abstract_classifier()

if CLASSIFIER_BACKEND != "atlas_shared":
    print(
        f"[triage_4d] atlas_shared unavailable -- using {CLASSIFIER_BACKEND}",
        file=sys.stderr,
    )


# ── Core decision function ────────────────────────────────────────────────────

def triage_one(
    paper: dict,
    *,
    classifier: Optional[Callable[[dict], float]] = None,
    voi_scorer: Optional[Callable[[dict], float]] = None,
) -> TriageRecord:
    """
    Triage a single paper dict and return a TriageRecord.

    Parameters
    ----------
    paper : dict
        Must contain 'reference_id' (or 'paper_id') and 'abstract'.
        Optional: 'title_raw'/'title', 'venue', 'doi', 'publication_year'.

    classifier : callable(paper -> float), optional
        Returns topic_confidence in [0.0, 1.0].
        Default: module-level _DEFAULT_CLASSIFIER.

    voi_scorer : callable(paper -> float), optional
        Returns voi_score in [0.0, 1.0].
        Default: cmr.voi_scoring.score_voi.

    Returns
    -------
    TriageRecord with all fields populated; triage_reason is never empty.
    """
    clf     = classifier or _DEFAULT_CLASSIFIER
    voi_fn  = voi_scorer or _default_voi_scorer

    paper_id = (
        paper.get("reference_id")
        or paper.get("paper_id")
        or paper.get("doi")
        or "<unknown>"
    )

    abstract = (paper.get("abstract") or "").strip()

    # ── Rule 1: Missing abstract ───────────────────────────────────────────────
    if not abstract:
        return TriageRecord(
            paper_id         = paper_id,
            triage_decision  = DECISION_MISSING_ABSTRACT,
            triage_reason    = "Abstract missing; classifier and VOI scoring skipped.",
            topic_confidence = None,
            voi_score        = None,
        )

    # ── Rule 2: Topic classification ──────────────────────────────────────────
    topic_confidence = float(clf(paper))

    if topic_confidence < TOPIC_THRESHOLD:
        return TriageRecord(
            paper_id         = paper_id,
            triage_decision  = DECISION_REJECT,
            triage_reason    = (
                f"Off-topic: classifier confidence {topic_confidence:.2f} is below "
                f"the {TOPIC_THRESHOLD} threshold."
            ),
            topic_confidence = topic_confidence,
            voi_score        = None,
        )

    # ── Rule 3: VOI scoring ───────────────────────────────────────────────────
    if voi_fn is None:
        # cmr.voi_scoring not importable -- degrade to EDGE_CASE
        return TriageRecord(
            paper_id         = paper_id,
            triage_decision  = DECISION_EDGE_CASE,
            triage_reason    = (
                f"Topic match confirmed (confidence {topic_confidence:.2f}); "
                "VOI scorer unavailable -- flagged for manual review."
            ),
            topic_confidence = topic_confidence,
            voi_score        = None,
        )

    voi_score = float(voi_fn(paper))

    if voi_score >= VOI_ACCEPT:
        reason = (
            f"Abstract strongly matches target topic (confidence {topic_confidence:.2f}) "
            f"and VOI score {voi_score:.2f} exceeds acceptance threshold {VOI_ACCEPT}."
        )
        decision = DECISION_ACCEPT

    elif voi_score >= VOI_EDGE:
        reason = (
            f"Topic match is moderate to strong (confidence {topic_confidence:.2f}); "
            f"VOI score {voi_score:.2f} is in the edge-case band "
            f"[{VOI_EDGE}, {VOI_ACCEPT}). Paper requires manual review."
        )
        decision = DECISION_EDGE_CASE

    else:
        reason = (
            f"Abstract is on-topic (confidence {topic_confidence:.2f}) but "
            f"VOI score {voi_score:.2f} is below the minimum threshold {VOI_EDGE}."
        )
        decision = DECISION_REJECT

    return TriageRecord(
        paper_id         = paper_id,
        triage_decision  = decision,
        triage_reason    = reason,
        topic_confidence = topic_confidence,
        voi_score        = voi_score,
    )


# ── Persistence layer ─────────────────────────────────────────────────────────

def _write_phase4d_columns(
    conn,
    reference_id: str,
    record: TriageRecord,
    new_stage: str,
) -> None:
    """Write Phase 4D decision fields to article_references."""
    conn.execute(
        """
        UPDATE article_references
           SET triage_stage             = ?,
               phase4d_decision        = ?,
               phase4d_reason          = ?,
               phase4d_topic_confidence= ?,
               phase4d_voi_score       = ?,
               phase4d_at              = ?
         WHERE reference_id = ?
        """,
        (
            new_stage,
            record.triage_decision,
            record.triage_reason,
            record.topic_confidence,
            record.voi_score,
            record.timestamp,
            reference_id,
        ),
    )


def _persist_accept(reference_id: str, record: TriageRecord, db_path: str) -> None:
    """ACCEPT: update triage_stage; phase4d columns recorded."""
    with get_connection(db_path) as conn:
        _write_phase4d_columns(conn, reference_id, record, TRIAGE_STAGE_ACCEPTED_FOR_DOWNLOAD)


def _persist_edge_case(reference_id: str, record: TriageRecord, db_path: str) -> None:
    """EDGE_CASE: update triage_stage + insert into edge_case_review_queue."""
    with get_connection(db_path) as conn:
        _write_phase4d_columns(conn, reference_id, record, TRIAGE_STAGE_EDGE_CASE_REVIEW)
        conn.execute(
            """
            INSERT OR REPLACE INTO edge_case_review_queue
                (reference_id, queued_at, manual_review_flag,
                 topic_confidence, voi_score, reason)
            VALUES (?, ?, 1, ?, ?, ?)
            """,
            (
                reference_id,
                record.timestamp,
                record.topic_confidence,
                record.voi_score,
                record.triage_reason,
            ),
        )


def _persist_reject(reference_id: str, record: TriageRecord, db_path: str) -> None:
    """REJECT: update triage_stage + append to triage_decision_log."""
    with get_connection(db_path) as conn:
        _write_phase4d_columns(conn, reference_id, record, TRIAGE_STAGE_REJECTED_AT_ABSTRACT)
        conn.execute(
            """
            INSERT INTO triage_decision_log
                (reference_id, decision, reason,
                 topic_confidence, voi_score, logged_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                reference_id,
                record.triage_decision,
                record.triage_reason,
                record.topic_confidence,
                record.voi_score,
                record.timestamp,
            ),
        )


def _persist_missing(reference_id: str, record: TriageRecord, db_path: str) -> None:
    """MISSING_ABSTRACT: update triage_stage + log; NOT treated as REJECT."""
    with get_connection(db_path) as conn:
        _write_phase4d_columns(conn, reference_id, record, TRIAGE_STAGE_REJECTED_AT_ABSTRACT)
        conn.execute(
            """
            INSERT INTO triage_decision_log
                (reference_id, decision, reason,
                 topic_confidence, voi_score, logged_at)
            VALUES (?, ?, ?, NULL, NULL, ?)
            """,
            (
                reference_id,
                DECISION_MISSING_ABSTRACT,
                record.triage_reason,
                record.timestamp,
            ),
        )


def _persist_record(reference_id: str, record: TriageRecord, db_path: str) -> None:
    """Route a TriageRecord to the correct persistence function."""
    if record.triage_decision == DECISION_ACCEPT:
        _persist_accept(reference_id, record, db_path)
    elif record.triage_decision == DECISION_EDGE_CASE:
        _persist_edge_case(reference_id, record, db_path)
    elif record.triage_decision == DECISION_REJECT:
        _persist_reject(reference_id, record, db_path)
    else:  # MISSING_ABSTRACT
        _persist_missing(reference_id, record, db_path)


# ── Batch processor ───────────────────────────────────────────────────────────

def run_phase4d_triage(
    papers: Optional[list[dict]] = None,
    db_path: str = LIFECYCLE_DB,
    *,
    classifier: Optional[Callable[[dict], float]] = None,
    voi_scorer: Optional[Callable[[dict], float]] = None,
    dry_run: bool = False,
    limit: Optional[int] = None,
) -> dict[str, int]:
    """
    Triage all 'abstract_collected' rows (or a supplied paper list).

    Parameters
    ----------
    papers    : explicit list of paper dicts; if None, reads from DB.
    db_path   : pipeline_lifecycle_full.db path.
    classifier: injectable classifier callable for testing.
    voi_scorer: injectable VOI scorer callable for testing.
    dry_run   : if True, run decisions but do not write to DB.
    limit     : cap the number of rows processed (for partial runs).

    Returns
    -------
    {
        'ACCEPT':           N,
        'EDGE_CASE':        N,
        'REJECT':           N,
        'MISSING_ABSTRACT': N,
        'total':            N,
    }
    """
    if not dry_run:
        init_lifecycle_db(db_path)

    if papers is None:
        rows = get_abstract_collected_rows(db_path)
    else:
        rows = list(papers)

    if limit is not None:
        rows = rows[:limit]

    counts: dict[str, int] = {
        DECISION_ACCEPT:           0,
        DECISION_EDGE_CASE:        0,
        DECISION_REJECT:           0,
        DECISION_MISSING_ABSTRACT: 0,
        "total":                   0,
    }

    total = len(rows)
    for i, paper in enumerate(rows, 1):
        reference_id = (
            paper.get("reference_id")
            or paper.get("paper_id")
            or f"<row-{i}>"
        )

        try:
            record = triage_one(paper, classifier=classifier, voi_scorer=voi_scorer)
        except Exception as exc:
            print(
                f"  [4d] ERROR on {reference_id}: {exc}",
                file=sys.stderr,
            )
            continue

        counts[record.triage_decision] += 1
        counts["total"] += 1

        if not dry_run:
            try:
                _persist_record(reference_id, record, db_path)
            except Exception as exc:
                print(
                    f"  [4d] PERSIST ERROR on {reference_id}: {exc}",
                    file=sys.stderr,
                )

        if i % 25 == 0 or i == total:
            print(
                f"  [4d {i:4d}/{total}] "
                f"ACCEPT={counts[DECISION_ACCEPT]}  "
                f"EDGE={counts[DECISION_EDGE_CASE]}  "
                f"REJECT={counts[DECISION_REJECT]}  "
                f"MISSING={counts[DECISION_MISSING_ABSTRACT]}"
            )

    if not dry_run:
        print(
            f"[phase4d] done. "
            f"ACCEPT={counts[DECISION_ACCEPT]}  "
            f"EDGE_CASE={counts[DECISION_EDGE_CASE]}  "
            f"REJECT={counts[DECISION_REJECT]}  "
            f"MISSING_ABSTRACT={counts[DECISION_MISSING_ABSTRACT]}  "
            f"total={counts['total']}"
        )

    return counts


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Phase 4D abstract triage -- classify each abstract for domain "
            "relevance and VOI, then persist the decision."
        )
    )
    parser.add_argument(
        "--db", default=LIFECYCLE_DB,
        help=f"lifecycle DB path (default: {LIFECYCLE_DB})"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run decisions but do not write to DB"
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Process at most N rows (for incremental runs)"
    )
    args = parser.parse_args()

    run_phase4d_triage(db_path=args.db, dry_run=args.dry_run, limit=args.limit)


if __name__ == "__main__":
    main()
