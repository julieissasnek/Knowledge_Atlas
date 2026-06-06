"""
test_phase4d_abstract_triage.py -- Contract tests for Phase 4D abstract triage.

Test-case index (maps to contract spec):
  Contract test 1: Decision field present and valid
  Contract test 2: Reason field non-empty
  Contract test 3: ACCEPT persistence (conf=0.92, voi=0.81)
  Contract test 4: EDGE_CASE storage and manual-review flag (conf=0.88, voi=0.63)
  Contract test 5: REJECT logging (conf=0.35)
  Contract test 6: MISSING_ABSTRACT handling (no classifier, no VOI)

Additional coverage:
  - All four decisions are possible
  - VOI thresholds: >= 0.70 ACCEPT, 0.50-0.69 EDGE_CASE, < 0.50 REJECT
  - MISSING_ABSTRACT never calls VOI scorer
  - REJECT papers appear in triage_decision_log
  - ACCEPT papers appear in article_references with correct triage_stage
  - EDGE_CASE papers appear in edge_case_review_queue with manual_review_flag=1
  - MISSING_ABSTRACT distinguishable from domain REJECT in log
  - Batch >= 100 papers without failure
  - score_voi() produces float in [0.0, 1.0]
  - AbstractRelevanceClassifier scores on-topic abstracts >= 0.65
  - dry_run does not write to DB
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

import pytest

# ── Path setup ─────────────────────────────────────────────────────────────────

TRACK2  = Path(__file__).resolve().parent
AE_SRC  = TRACK2.parent.parent.parent.parent / "Article_Eater" / "src"
if str(AE_SRC) not in sys.path:
    sys.path.insert(0, str(AE_SRC))

# ── Module imports ─────────────────────────────────────────────────────────────

from abstract_triage_4d import (
    DECISION_ACCEPT,
    DECISION_EDGE_CASE,
    DECISION_MISSING_ABSTRACT,
    DECISION_REJECT,
    TOPIC_THRESHOLD,
    VOI_ACCEPT,
    VOI_EDGE,
    AbstractRelevanceClassifier,
    TriageRecord,
    _persist_record,
    run_phase4d_triage,
    triage_one,
)
from cmr.voi_scoring import score_voi  # type: ignore
from lifecycle_db import (
    TRIAGE_STAGE_ACCEPTED_FOR_DOWNLOAD,
    TRIAGE_STAGE_EDGE_CASE_REVIEW,
    TRIAGE_STAGE_REJECTED_AT_ABSTRACT,
    get_connection,
    init_lifecycle_db,
    update_triage_stage,
    upsert_reference,
    TRIAGE_STAGE_ABSTRACT_COLLECTED,
)

# ── Test fixtures ──────────────────────────────────────────────────────────────

GOOD_ABSTRACT = (
    "This randomized controlled study examines the effects of daylight exposure "
    "on cognitive performance, sustained attention, and working memory in open-plan "
    "offices. Sixty participants were assessed under three lighting conditions: "
    "natural daylight, warm LED, and cool LED. Results show significant improvement "
    "in alertness and task performance under circadian-aligned daylight exposure, "
    "consistent with circadian rhythm entrainment theory. Implications for biophilic "
    "office design are discussed."
)  # heavily on-topic, empirical, long

OFF_TOPIC_ABSTRACT = (
    "We introduce a transformer model for natural language processing that uses "
    "multi-head attention to encode long-range dependencies in text. The architecture "
    "achieves state-of-the-art results on the GLUE benchmark without domain-specific "
    "pre-training. We analyze the information flow through attention layers and "
    "demonstrate that the model generalises across diverse linguistic tasks."
)

VALID_DOI = "10.1016/j.buildenv.2022.109000"


def _clf(conf: float):
    """Return a mock classifier that always returns `conf`."""
    return lambda paper: conf


def _voi(score: float):
    """Return a mock VOI scorer that always returns `score`."""
    return lambda paper: score


def _make_paper(
    *,
    abstract: str = GOOD_ABSTRACT,
    ref_id: str = "REF-TEST-000001",
    doi: str = VALID_DOI,
    title: str = "Daylight and cognitive performance in offices",
    year: int = 2022,
    venue: str = "Building and Environment",
) -> dict:
    return {
        "reference_id":   ref_id,
        "doi":            doi,
        "title_raw":      title,
        "abstract":       abstract,
        "venue":          venue,
        "publication_year": year,
    }


def _seed_abstract_collected_row(
    db: str,
    *,
    doi: str = VALID_DOI,
    title: str = "Daylight and cognitive performance",
    abstract: str = GOOD_ABSTRACT,
    year: int = 2022,
) -> str:
    """Insert a row, advance it to abstract_collected, return reference_id."""
    init_lifecycle_db(db)
    _, ref_id = upsert_reference(
        {
            "title_raw":      title,
            "doi":            doi,
            "discovered_via": "serpapi_scholar",
        },
        db_path=db,
    )
    update_triage_stage(
        ref_id, TRIAGE_STAGE_ABSTRACT_COLLECTED,
        abstract=abstract,
        abstract_source="semantic_scholar",
        db_path=db,
    )
    # Write the abstract directly to the column too
    with get_connection(db) as conn:
        conn.execute(
            "UPDATE article_references SET abstract = ? WHERE reference_id = ?",
            (abstract, ref_id),
        )
    return ref_id


# ===========================================================================
# Contract test 1: Decision field present and valid
# ===========================================================================

class TestDecisionFieldPresent:
    """Every processed paper must carry a valid triage_decision."""

    @pytest.mark.parametrize("conf,voi_val,expected", [
        (0.92, 0.81, DECISION_ACCEPT),
        (0.88, 0.63, DECISION_EDGE_CASE),
        (0.88, 0.40, DECISION_REJECT),
        (0.35, None, DECISION_REJECT),
    ])
    def test_decision_field_valid(self, conf, voi_val, expected):
        voi_fn = _voi(voi_val) if voi_val is not None else _voi(0.0)
        paper  = _make_paper()
        record = triage_one(paper, classifier=_clf(conf), voi_scorer=voi_fn)
        assert record.triage_decision in (
            DECISION_ACCEPT, DECISION_EDGE_CASE, DECISION_REJECT, DECISION_MISSING_ABSTRACT
        )
        assert record.triage_decision == expected

    def test_missing_abstract_decision(self):
        paper  = _make_paper(abstract="")
        record = triage_one(paper, classifier=_clf(0.90), voi_scorer=_voi(0.80))
        assert record.triage_decision == DECISION_MISSING_ABSTRACT

    def test_whitespace_only_abstract_is_missing(self):
        paper  = _make_paper(abstract="   \t\n  ")
        record = triage_one(paper, classifier=_clf(0.90), voi_scorer=_voi(0.80))
        assert record.triage_decision == DECISION_MISSING_ABSTRACT


# ===========================================================================
# Contract test 2: Reason field non-empty
# ===========================================================================

class TestReasonFieldPresent:
    """Every decision must include a non-empty triage_reason."""

    @pytest.mark.parametrize("conf,voi_val", [
        (0.92, 0.81),   # ACCEPT
        (0.88, 0.63),   # EDGE_CASE
        (0.88, 0.40),   # REJECT (on-topic, low VOI)
        (0.35, 0.80),   # REJECT (off-topic)
    ])
    def test_reason_non_empty_for_scored_papers(self, conf, voi_val):
        paper  = _make_paper()
        record = triage_one(paper, classifier=_clf(conf), voi_scorer=_voi(voi_val))
        assert len(record.triage_reason) > 0

    def test_reason_non_empty_for_missing_abstract(self):
        paper  = _make_paper(abstract="")
        record = triage_one(paper, classifier=_clf(0.90), voi_scorer=_voi(0.80))
        assert len(record.triage_reason) > 0

    def test_reason_references_confidence_for_reject(self):
        paper  = _make_paper()
        record = triage_one(paper, classifier=_clf(0.35), voi_scorer=_voi(0.80))
        assert "0.35" in record.triage_reason or "threshold" in record.triage_reason.lower()

    def test_reason_references_voi_for_accept(self):
        paper  = _make_paper()
        record = triage_one(paper, classifier=_clf(0.92), voi_scorer=_voi(0.81))
        assert "0.81" in record.triage_reason or "VOI" in record.triage_reason


# ===========================================================================
# Contract test 3: ACCEPT persistence (conf=0.92, voi=0.81)
# ===========================================================================

class TestAcceptPersistence:
    """ACCEPT papers must appear in the lifecycle DB with correct triage_stage."""

    def test_accept_decision(self):
        paper  = _make_paper()
        record = triage_one(paper, classifier=_clf(0.92), voi_scorer=_voi(0.81))
        assert record.triage_decision == DECISION_ACCEPT

    def test_accept_scores_recorded(self):
        paper  = _make_paper()
        record = triage_one(paper, classifier=_clf(0.92), voi_scorer=_voi(0.81))
        assert record.topic_confidence == pytest.approx(0.92)
        assert record.voi_score        == pytest.approx(0.81)

    def test_accept_persisted_to_db(self, tmp_path):
        db     = str(tmp_path / "lc.db")
        ref_id = _seed_abstract_collected_row(db)
        paper  = {**_make_paper(ref_id=ref_id), "reference_id": ref_id}

        record = triage_one(paper, classifier=_clf(0.92), voi_scorer=_voi(0.81))
        _persist_record(ref_id, record, db)

        with get_connection(db) as conn:
            row = conn.execute(
                "SELECT triage_stage, phase4d_decision FROM article_references "
                "WHERE reference_id = ?", (ref_id,),
            ).fetchone()

        assert row["triage_stage"]    == TRIAGE_STAGE_ACCEPTED_FOR_DOWNLOAD
        assert row["phase4d_decision"] == DECISION_ACCEPT

    def test_accept_reason_mentions_topic_and_voi(self):
        paper  = _make_paper()
        record = triage_one(paper, classifier=_clf(0.92), voi_scorer=_voi(0.81))
        reason = record.triage_reason.lower()
        assert any(kw in reason for kw in ("topic", "confidence", "match"))
        assert any(kw in reason for kw in ("voi", "0.81", "value"))


# ===========================================================================
# Contract test 4: EDGE_CASE storage (conf=0.88, voi=0.63)
# ===========================================================================

class TestEdgeCaseStorage:
    """EDGE_CASE papers must be stored separately and flagged for review."""

    def test_edge_case_decision(self):
        paper  = _make_paper()
        record = triage_one(paper, classifier=_clf(0.88), voi_scorer=_voi(0.63))
        assert record.triage_decision == DECISION_EDGE_CASE

    def test_edge_case_stored_in_queue(self, tmp_path):
        db     = str(tmp_path / "lc.db")
        ref_id = _seed_abstract_collected_row(db)
        paper  = {**_make_paper(ref_id=ref_id), "reference_id": ref_id}

        record = triage_one(paper, classifier=_clf(0.88), voi_scorer=_voi(0.63))
        _persist_record(ref_id, record, db)

        with get_connection(db) as conn:
            row = conn.execute(
                "SELECT * FROM edge_case_review_queue WHERE reference_id = ?",
                (ref_id,),
            ).fetchone()

        assert row is not None
        assert row["manual_review_flag"] == 1

    def test_edge_case_triage_stage_updated(self, tmp_path):
        db     = str(tmp_path / "lc.db")
        ref_id = _seed_abstract_collected_row(db)
        paper  = {**_make_paper(ref_id=ref_id), "reference_id": ref_id}

        record = triage_one(paper, classifier=_clf(0.88), voi_scorer=_voi(0.63))
        _persist_record(ref_id, record, db)

        with get_connection(db) as conn:
            stage = conn.execute(
                "SELECT triage_stage FROM article_references WHERE reference_id = ?",
                (ref_id,),
            ).fetchone()["triage_stage"]

        assert stage == TRIAGE_STAGE_EDGE_CASE_REVIEW

    def test_edge_case_not_in_accept_stage(self, tmp_path):
        db     = str(tmp_path / "lc.db")
        ref_id = _seed_abstract_collected_row(db)
        paper  = {**_make_paper(ref_id=ref_id), "reference_id": ref_id}

        record = triage_one(paper, classifier=_clf(0.88), voi_scorer=_voi(0.63))
        _persist_record(ref_id, record, db)

        with get_connection(db) as conn:
            stage = conn.execute(
                "SELECT triage_stage FROM article_references WHERE reference_id = ?",
                (ref_id,),
            ).fetchone()["triage_stage"]

        assert stage != TRIAGE_STAGE_ACCEPTED_FOR_DOWNLOAD


# ===========================================================================
# Contract test 5: REJECT logging (conf=0.35)
# ===========================================================================

class TestRejectLogging:
    """REJECT records must be logged and auditable; not silently dropped."""

    def test_low_confidence_is_rejected(self):
        paper  = _make_paper()
        record = triage_one(paper, classifier=_clf(0.35), voi_scorer=_voi(0.90))
        assert record.triage_decision == DECISION_REJECT

    def test_reject_written_to_log(self, tmp_path):
        db     = str(tmp_path / "lc.db")
        ref_id = _seed_abstract_collected_row(db)
        paper  = {**_make_paper(ref_id=ref_id), "reference_id": ref_id}

        record = triage_one(paper, classifier=_clf(0.35), voi_scorer=_voi(0.90))
        _persist_record(ref_id, record, db)

        with get_connection(db) as conn:
            row = conn.execute(
                "SELECT * FROM triage_decision_log WHERE reference_id = ?",
                (ref_id,),
            ).fetchone()

        assert row is not None
        assert row["decision"] == DECISION_REJECT
        assert len(row["reason"]) > 0

    def test_reject_not_in_accept_stage(self, tmp_path):
        db     = str(tmp_path / "lc.db")
        ref_id = _seed_abstract_collected_row(db)
        paper  = {**_make_paper(ref_id=ref_id), "reference_id": ref_id}

        record = triage_one(paper, classifier=_clf(0.35), voi_scorer=_voi(0.90))
        _persist_record(ref_id, record, db)

        with get_connection(db) as conn:
            stage = conn.execute(
                "SELECT triage_stage FROM article_references WHERE reference_id = ?",
                (ref_id,),
            ).fetchone()["triage_stage"]

        assert stage == TRIAGE_STAGE_REJECTED_AT_ABSTRACT

    def test_reject_not_in_edge_case_queue(self, tmp_path):
        db     = str(tmp_path / "lc.db")
        ref_id = _seed_abstract_collected_row(db)
        paper  = {**_make_paper(ref_id=ref_id), "reference_id": ref_id}

        record = triage_one(paper, classifier=_clf(0.35), voi_scorer=_voi(0.90))
        _persist_record(ref_id, record, db)

        with get_connection(db) as conn:
            row = conn.execute(
                "SELECT * FROM edge_case_review_queue WHERE reference_id = ?",
                (ref_id,),
            ).fetchone()

        assert row is None


# ===========================================================================
# Contract test 6: MISSING_ABSTRACT handling
# ===========================================================================

class TestMissingAbstractHandling:
    """MISSING_ABSTRACT must bypass scoring, be logged, and stay distinct from REJECT."""

    def test_null_abstract_gives_missing(self):
        paper  = _make_paper(abstract="")
        voi_mock = MagicMock(return_value=0.80)
        record = triage_one(paper, classifier=_clf(0.90), voi_scorer=voi_mock)
        assert record.triage_decision == DECISION_MISSING_ABSTRACT

    def test_voi_not_called_for_missing_abstract(self):
        paper     = _make_paper(abstract="")
        voi_mock  = MagicMock(return_value=0.80)
        clf_mock  = MagicMock(return_value=0.90)
        triage_one(paper, classifier=clf_mock, voi_scorer=voi_mock)
        voi_mock.assert_not_called()

    def test_classifier_not_called_for_missing_abstract(self):
        paper    = _make_paper(abstract="")
        clf_mock = MagicMock(return_value=0.90)
        triage_one(paper, classifier=clf_mock, voi_scorer=_voi(0.80))
        clf_mock.assert_not_called()

    def test_missing_abstract_has_reason(self):
        paper  = _make_paper(abstract="")
        record = triage_one(paper, classifier=_clf(0.90), voi_scorer=_voi(0.80))
        assert len(record.triage_reason) > 0
        assert any(kw in record.triage_reason.lower()
                   for kw in ("abstract", "missing", "unavailable", "skipped"))

    def test_missing_abstract_logged_not_silently_dropped(self, tmp_path):
        db     = str(tmp_path / "lc.db")
        ref_id = _seed_abstract_collected_row(db, abstract="")
        paper  = {**_make_paper(ref_id=ref_id, abstract=""), "reference_id": ref_id}

        record = triage_one(paper, classifier=_clf(0.90), voi_scorer=_voi(0.80))
        _persist_record(ref_id, record, db)

        with get_connection(db) as conn:
            log_row = conn.execute(
                "SELECT decision FROM triage_decision_log WHERE reference_id = ?",
                (ref_id,),
            ).fetchone()

        assert log_row is not None
        assert log_row["decision"] == DECISION_MISSING_ABSTRACT

    def test_missing_abstract_distinguishable_from_reject(self, tmp_path):
        """MISSING_ABSTRACT and domain REJECT must produce different decision values."""
        db = str(tmp_path / "lc.db")

        ref_missing = _seed_abstract_collected_row(db, doi="10.1000/miss", abstract="")
        ref_reject  = _seed_abstract_collected_row(
            db, doi="10.1000/rej",
            abstract=OFF_TOPIC_ABSTRACT,
        )

        p_missing = {**_make_paper(ref_id=ref_missing, abstract=""), "reference_id": ref_missing}
        p_reject  = {**_make_paper(ref_id=ref_reject,  abstract=OFF_TOPIC_ABSTRACT), "reference_id": ref_reject}

        r_missing = triage_one(p_missing, classifier=_clf(0.90), voi_scorer=_voi(0.80))
        r_reject  = triage_one(p_reject,  classifier=_clf(0.20), voi_scorer=_voi(0.80))

        assert r_missing.triage_decision == DECISION_MISSING_ABSTRACT
        assert r_reject.triage_decision  == DECISION_REJECT
        assert r_missing.triage_decision != r_reject.triage_decision


# ===========================================================================
# VOI threshold enforcement
# ===========================================================================

class TestVOIThresholds:
    """VOI thresholds must be enforced consistently."""

    @pytest.mark.parametrize("voi_val,expected", [
        (0.70, DECISION_ACCEPT),    # exactly at floor
        (0.75, DECISION_ACCEPT),
        (1.00, DECISION_ACCEPT),
        (0.69, DECISION_EDGE_CASE), # just below ACCEPT floor
        (0.50, DECISION_EDGE_CASE), # exactly at EDGE floor
        (0.60, DECISION_EDGE_CASE),
        (0.49, DECISION_REJECT),    # just below EDGE floor
        (0.30, DECISION_REJECT),
        (0.00, DECISION_REJECT),
    ])
    def test_voi_threshold(self, voi_val, expected):
        paper  = _make_paper()
        record = triage_one(paper, classifier=_clf(0.80), voi_scorer=_voi(voi_val))
        assert record.triage_decision == expected

    def test_topic_threshold_boundary(self):
        """Exactly at TOPIC_THRESHOLD passes; just below fails."""
        paper = _make_paper()
        just_below = round(TOPIC_THRESHOLD - 0.001, 4)
        at_threshold = TOPIC_THRESHOLD

        r_fail = triage_one(paper, classifier=_clf(just_below), voi_scorer=_voi(0.80))
        r_pass = triage_one(paper, classifier=_clf(at_threshold), voi_scorer=_voi(0.80))

        assert r_fail.triage_decision == DECISION_REJECT
        assert r_pass.triage_decision != DECISION_REJECT

    def test_voi_score_none_for_off_topic(self):
        """Off-topic papers (conf < threshold) must not have a VOI score."""
        paper  = _make_paper()
        record = triage_one(paper, classifier=_clf(0.40), voi_scorer=_voi(0.90))
        assert record.voi_score is None

    def test_voi_score_none_for_missing_abstract(self):
        paper  = _make_paper(abstract="")
        record = triage_one(paper, classifier=_clf(0.90), voi_scorer=_voi(0.90))
        assert record.voi_score is None


# ===========================================================================
# Batch: >= 100 papers without failure
# ===========================================================================

class TestBatchScale:
    """The module must triage >= 100 papers in one run without errors."""

    def test_100_papers_no_error(self, tmp_path):
        papers = [
            _make_paper(
                ref_id=f"REF-BATCH-{i:06d}",
                doi=f"10.1000/batch{i}",
                abstract=GOOD_ABSTRACT if i % 3 != 0 else OFF_TOPIC_ABSTRACT,
            )
            for i in range(100)
        ]
        # Use injected classifiers so no network is needed
        confs = [0.90 if i % 3 != 0 else 0.30 for i in range(100)]
        idx = [0]

        def cycling_clf(paper):
            v = confs[idx[0] % 100]
            idx[0] += 1
            return v

        counts = run_phase4d_triage(
            papers,
            db_path=str(tmp_path / "lc.db"),
            classifier=cycling_clf,
            voi_scorer=_voi(0.75),
            dry_run=True,
        )

        assert counts["total"] == 100
        assert sum(
            counts[d]
            for d in (DECISION_ACCEPT, DECISION_EDGE_CASE,
                       DECISION_REJECT, DECISION_MISSING_ABSTRACT)
        ) == 100

    def test_all_decisions_accounted_for(self, tmp_path):
        """Sum of all four decision buckets must equal total."""
        papers = [_make_paper(ref_id=f"REF-{i}", doi=f"10.9/t{i}") for i in range(10)]
        counts = run_phase4d_triage(
            papers,
            db_path=str(tmp_path / "lc.db"),
            classifier=_clf(0.80),
            voi_scorer=_voi(0.72),
            dry_run=True,
        )
        bucket_sum = (
            counts[DECISION_ACCEPT] + counts[DECISION_EDGE_CASE]
            + counts[DECISION_REJECT] + counts[DECISION_MISSING_ABSTRACT]
        )
        assert bucket_sum == counts["total"]


# ===========================================================================
# AbstractRelevanceClassifier
# ===========================================================================

class TestAbstractRelevanceClassifier:
    """AbstractRelevanceClassifier must score on-topic abstracts >= 0.65."""

    def _classify(self, title: str = "", abstract: str = "", venue: str = "") -> float:
        from abstract_triage_4d import _ClassificationEvidence
        clf = AbstractRelevanceClassifier()
        ev  = _ClassificationEvidence(title=title, abstract=abstract, venue=venue)
        return clf.classify(ev)

    def test_on_topic_abstract_passes_threshold(self):
        score = self._classify(
            title    = "Daylight and cognitive performance in offices",
            abstract = GOOD_ABSTRACT,
            venue    = "Building and Environment",
        )
        assert score >= TOPIC_THRESHOLD

    def test_off_topic_abstract_fails_threshold(self):
        score = self._classify(
            title    = "Transformer models for NLP tasks",
            abstract = OFF_TOPIC_ABSTRACT,
        )
        assert score < TOPIC_THRESHOLD

    def test_empty_abstract_scores_zero(self):
        score = self._classify(title="", abstract="", venue="")
        assert score == pytest.approx(0.0)

    def test_score_clamped_to_one(self):
        very_rich = " ".join([
            "daylight daylighting natural light circadian cognitive cognition",
            "attention working memory alertness academic performance circadian",
            "biophilic thermal comfort indoor environment quality daylight",
        ])
        score = self._classify(title=very_rich, abstract=very_rich)
        assert score <= 1.0

    def test_venue_bonus_applied(self):
        title = "A study on light and health"
        abstract = "We measured daylight and cognitive performance."
        score_no_venue  = self._classify(title=title, abstract=abstract, venue="")
        score_with_venue = self._classify(title=title, abstract=abstract, venue="daylighting research quarterly")
        assert score_with_venue > score_no_venue


# ===========================================================================
# score_voi (cmr.voi_scoring)
# ===========================================================================

class TestScoreVoi:
    """score_voi must return float in [0.0, 1.0] and respond to evidence."""

    def test_returns_float(self):
        paper = _make_paper()
        assert isinstance(score_voi(paper), float)

    def test_in_valid_range(self):
        paper = _make_paper()
        s = score_voi(paper)
        assert 0.0 <= s <= 1.0

    def test_empty_paper_does_not_raise(self):
        s = score_voi({})
        assert 0.0 <= s <= 1.0

    def test_rich_paper_scores_higher_than_empty(self):
        rich  = score_voi(_make_paper())
        empty = score_voi({})
        assert rich > empty

    def test_recent_paper_scores_higher_than_old(self):
        new_paper = _make_paper(year=2022)
        old_paper = _make_paper(year=1995)
        assert score_voi(new_paper) >= score_voi(old_paper)

    def test_doi_presence_increases_score(self):
        with_doi    = score_voi(_make_paper(doi="10.1/test"))
        without_doi = score_voi({**_make_paper(), "doi": ""})
        assert with_doi >= without_doi


# ===========================================================================
# dry_run: no writes to DB
# ===========================================================================

class TestDryRun:
    """dry_run=True must not modify the database."""

    def test_dry_run_leaves_db_unchanged(self, tmp_path):
        db     = str(tmp_path / "lc.db")
        ref_id = _seed_abstract_collected_row(db)
        paper  = {**_make_paper(ref_id=ref_id), "reference_id": ref_id}

        run_phase4d_triage(
            [paper],
            db_path=db,
            classifier=_clf(0.92),
            voi_scorer=_voi(0.81),
            dry_run=True,
        )

        with get_connection(db) as conn:
            row = conn.execute(
                "SELECT triage_stage, phase4d_decision FROM article_references "
                "WHERE reference_id = ?", (ref_id,),
            ).fetchone()

        # triage_stage must still be abstract_collected, not accepted_for_download
        assert row["triage_stage"]     == TRIAGE_STAGE_ABSTRACT_COLLECTED
        assert row["phase4d_decision"] is None


# ===========================================================================
# TriageRecord invariants
# ===========================================================================

class TestTriageRecordInvariants:
    """TriageRecord enforces its own invariants via __post_init__."""

    def test_valid_decisions_accepted(self):
        for d in (DECISION_ACCEPT, DECISION_EDGE_CASE, DECISION_REJECT, DECISION_MISSING_ABSTRACT):
            r = TriageRecord(
                paper_id=f"REF-{d}",
                triage_decision=d,
                triage_reason="test",
                topic_confidence=0.8,
                voi_score=0.7,
            )
            assert r.triage_decision == d

    def test_invalid_decision_raises(self):
        with pytest.raises(AssertionError):
            TriageRecord(
                paper_id="X",
                triage_decision="UNKNOWN_DECISION",
                triage_reason="test",
                topic_confidence=0.8,
                voi_score=0.7,
            )

    def test_empty_reason_raises(self):
        with pytest.raises(AssertionError):
            TriageRecord(
                paper_id="X",
                triage_decision=DECISION_ACCEPT,
                triage_reason="",
                topic_confidence=0.8,
                voi_score=0.7,
            )

    def test_timestamp_auto_populated(self):
        r = TriageRecord(
            paper_id="X",
            triage_decision=DECISION_ACCEPT,
            triage_reason="ok",
            topic_confidence=0.8,
            voi_score=0.7,
        )
        assert r.timestamp  # not empty
        assert "T" in r.timestamp  # ISO 8601 format
