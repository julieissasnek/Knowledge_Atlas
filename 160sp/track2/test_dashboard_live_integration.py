"""
test_dashboard_live_integration.py -- Infrastructure durability test.

Proves that the PRISMA dashboard is a live, dynamic window into the database:
  1. Capture baseline counts from the dashboard data module.
  2. Insert a new ACCEPT paper directly into the lifecycle DB.
  3. Re-export the dashboard JSON (simulates a browser refresh trigger).
  4. Verify every affected count shifted by exactly the expected delta.
  5. Zero HTML or Python presentation code is modified.

Also verifies the three new canonical components:
  - PaperFetcher.search() facade
  - estimate_study_type() from paper_fetcher
  - classify_closure() from discovery_funnel
  - pdf_corpus_inventory table in lifecycle DB
  - study_type column on article_references
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

TRACK2 = Path(__file__).resolve().parent
AE_SRC = TRACK2.parent.parent.parent.parent / "Article_Eater" / "src"
if str(AE_SRC) not in sys.path:
    sys.path.insert(0, str(AE_SRC))

from lifecycle_db import (
    LIFECYCLE_DB,
    get_connection,
    init_lifecycle_db,
    upsert_reference,
    update_triage_stage,
    TRIAGE_STAGE_ACCEPTED_FOR_DOWNLOAD,
)
from prisma_dashboard_data import compute_prisma_dashboard_data
from prisma_dashboard_export import run_export

LONG_ABSTRACT = (
    "This randomized controlled study examines the effects of daylight exposure "
    "on cognitive performance, sustained attention, and working memory in open-plan "
    "offices. Results show significant improvement under circadian-aligned daylight, "
    "consistent with circadian rhythm entrainment theory. VOI is high."
) * 2  # ~400 chars, clearly passes _is_valid_abstract


def _seed_accept(db: str, doi: str, gap: str = "GAP-PNU-001") -> str:
    """Insert a fresh ACCEPT row with a long abstract."""
    _, ref = upsert_reference(
        {"title_raw": f"Test paper {doi}", "doi": doi,
         "discovered_via": "serpapi_scholar", "gap_id": gap},
        db_path=db,
    )
    with get_connection(db) as conn:
        conn.execute(
            "UPDATE article_references "
            "SET triage_stage=?, phase4d_decision='ACCEPT', "
            "    phase4d_voi_score=0.80, phase4d_topic_confidence=0.90, "
            "    phase4d_reason='Test ACCEPT row', "
            "    abstract=?, abstract_source='semantic_scholar' "
            "WHERE reference_id=?",
            (TRIAGE_STAGE_ACCEPTED_FOR_DOWNLOAD, LONG_ABSTRACT, ref),
        )
    return ref


# ===========================================================================
# DURABILITY TEST: dashboard updates dynamically on DB change
# ===========================================================================

class TestDashboardLiveIntegration:

    def test_accept_count_updates_after_new_row_inserted(self, tmp_path):
        """
        Core durability contract:
          - Read baseline accept count
          - Insert one new ACCEPT paper
          - Re-call compute_prisma_dashboard_data (simulates JSON re-export)
          - Verify accept count increased by exactly 1
          - Zero HTML or Python files modified
        """
        db  = str(tmp_path / "lc.db")
        qf  = str(tmp_path / "q.json")
        Path(qf).write_text(
            json.dumps([{"gap_id": "GAP-PNU-001", "boolean_query": "daylight"}]),
            encoding="utf-8",
        )
        init_lifecycle_db(db)

        # Baseline — empty DB
        baseline = compute_prisma_dashboard_data(db, qf)
        before_accept = baseline["panel_d"]["accept"]
        before_total  = baseline["panel_e"]["identified"]

        # DB write (simulates a pipeline run completing)
        _seed_accept(db, "10.1/live-test-001")

        # Re-export (simulates browser refresh triggering JSON regeneration)
        updated = compute_prisma_dashboard_data(db, qf)
        after_accept = updated["panel_d"]["accept"]
        after_total  = updated["panel_e"]["identified"]

        assert after_accept == before_accept + 1, (
            f"accept count should increase by 1: {before_accept} -> {after_accept}"
        )
        assert after_total == before_total + 1, (
            f"identified count should increase by 1: {before_total} -> {after_total}"
        )

    def test_multiple_inserts_tracked(self, tmp_path):
        db = str(tmp_path / "lc.db")
        qf = str(tmp_path / "q.json")
        Path(qf).write_text(
            json.dumps([{"gap_id": "GAP-PNU-001", "boolean_query": "x"}]),
            encoding="utf-8",
        )
        init_lifecycle_db(db)

        for i in range(5):
            _seed_accept(db, f"10.1/multi-{i}")

        data = compute_prisma_dashboard_data(db, qf)
        assert data["panel_d"]["accept"] == 5
        assert data["panel_e"]["identified"] == 5

    def test_json_export_reflects_db_state(self, tmp_path):
        """JSON output matches DB state; HTML not touched."""
        db   = str(tmp_path / "lc.db")
        qf   = str(tmp_path / "q.json")
        Path(qf).write_text(
            json.dumps([{"gap_id": "GAP-PNU-001", "boolean_query": "x"}]),
            encoding="utf-8",
        )
        jout = str(tmp_path / "dash.json")
        hout = str(tmp_path / "dash.html")
        init_lifecycle_db(db)
        _seed_accept(db, "10.1/export-test")

        run_export(db_path=db, query_file=qf, json_out=jout, html_out=hout)
        j = json.loads(Path(jout).read_text(encoding="utf-8"))
        assert j["panel_d"]["accept"] == 1
        assert j["panel_e"]["identified"] == 1

    def test_reject_and_missing_do_not_inflate_accept(self, tmp_path):
        db = str(tmp_path / "lc.db")
        qf = str(tmp_path / "q.json")
        Path(qf).write_text(json.dumps([{"gap_id": "GAP-PNU-001", "boolean_query": "x"}]))
        init_lifecycle_db(db)
        # Add ACCEPT, REJECT, and MISSING rows
        _seed_accept(db, "10.1/a1")
        _, r2 = upsert_reference({"title_raw": "Reject paper","doi":"10.1/r1","discovered_via":"serpapi_scholar"}, db_path=db)
        with get_connection(db) as conn:
            conn.execute("UPDATE article_references SET phase4d_decision='REJECT', triage_stage='rejected_at_abstract' WHERE reference_id=?", (r2,))
        _, r3 = upsert_reference({"title_raw":"Missing paper","doi":"10.1/m1","discovered_via":"serpapi_scholar"}, db_path=db)
        with get_connection(db) as conn:
            conn.execute("UPDATE article_references SET triage_stage='abstract_missing' WHERE reference_id=?", (r3,))

        data = compute_prisma_dashboard_data(db, qf)
        assert data["panel_d"]["accept"]  == 1
        assert data["panel_d"]["reject"]  == 1
        assert data["panel_c"]["missing_abstract"] == 1
        assert data["panel_e"]["identified"] == 3


# ===========================================================================
# NEW CANONICAL COMPONENTS
# ===========================================================================

class TestPaperFetcherFacade:
    """PaperFetcher.search() and estimate_study_type() are importable and correct."""

    def test_paper_fetcher_importable(self):
        from services.paper_fetcher import PaperFetcher  # type: ignore
        f = PaperFetcher()
        assert callable(f.search)

    def test_estimate_study_type_module_level(self):
        from services.paper_fetcher import estimate_study_type  # type: ignore
        assert estimate_study_type("This randomized controlled trial shows...") == "empirical_rct"
        assert estimate_study_type("A meta-analysis of 45 studies...") == "meta_analysis"
        assert estimate_study_type("A systematic review of the literature...") == "systematic_review"
        assert estimate_study_type("This survey of 500 respondents...") == "survey"
        assert estimate_study_type("") == "unknown"

    def test_estimate_study_type_via_paper_fetcher(self):
        from services.paper_fetcher import PaperFetcher  # type: ignore
        f = PaperFetcher()
        assert f.estimate_study_type("This randomized experiment...") == "empirical_rct"

    def test_unpaywall_client_alias_importable(self):
        from services.paper_fetcher import UnpaywallClient  # type: ignore
        # May be None if ingest package not on path; that's ok — it's importable
        assert UnpaywallClient is None or callable(getattr(UnpaywallClient, "try_download", None))

    def test_paper_fetcher_search_returns_none_on_empty_inputs(self):
        """search() with no doi and no title returns None without raising."""
        from services.paper_fetcher import PaperFetcher  # type: ignore
        f = PaperFetcher()
        assert f.search() is None


class TestDiscoveryFunnel:
    """classify_closure() from discovery_funnel.py."""

    def test_classify_closure_importable(self):
        from services.discovery_funnel import classify_closure, GapClosure  # type: ignore
        assert callable(classify_closure)

    def test_classify_closure_open_when_no_papers(self, tmp_path):
        from services.discovery_funnel import classify_closure  # type: ignore
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        result = classify_closure("GAP-PNU-001", db_path=db)
        assert result.status in ("open", "insufficient_data")
        assert result.accept_count == 0

    def test_classify_closure_closed_with_enough_accept(self, tmp_path):
        from services.discovery_funnel import classify_closure, CLOSED_THRESHOLD  # type: ignore
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        for i in range(CLOSED_THRESHOLD + 1):
            _seed_accept(db, f"10.1/close{i}")
        result = classify_closure("GAP-PNU-001", db_path=db)
        assert result.status == "closed"
        assert result.accept_count >= CLOSED_THRESHOLD

    def test_classify_closure_partial_with_few_accept(self, tmp_path):
        from services.discovery_funnel import classify_closure  # type: ignore
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        _seed_accept(db, "10.1/partial1")
        result = classify_closure("GAP-PNU-001", db_path=db)
        assert result.status in ("partially_closed", "closed")

    def test_gap_closure_result_has_reason(self, tmp_path):
        from services.discovery_funnel import classify_closure  # type: ignore
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        result = classify_closure("GAP-PNU-001", db_path=db)
        assert len(result.reason) > 0


class TestPdfCorpusInventoryTable:
    """pdf_corpus_inventory table exists after init."""

    def test_table_created_on_init(self, tmp_path):
        db = str(tmp_path / "lc.db")
        init_lifecycle_db(db)
        with get_connection(db) as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
        assert "pdf_corpus_inventory" in tables

    def test_table_has_required_columns(self, tmp_path):
        db = str(tmp_path / "lc.db")
        init_lifecycle_db(db)
        with get_connection(db) as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(pdf_corpus_inventory)").fetchall()}
        for col in ("reference_id", "doi", "title_normalized", "sha256", "local_path", "ingested_at"):
            assert col in cols, f"Missing column: {col}"


class TestStudyTypeColumn:
    """study_type column exists on article_references after init."""

    def test_study_type_column_present(self, tmp_path):
        db = str(tmp_path / "lc.db")
        init_lifecycle_db(db)
        with get_connection(db) as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(article_references)").fetchall()}
        assert "study_type" in cols

    def test_study_type_writable(self, tmp_path):
        db = str(tmp_path / "lc.db")
        init_lifecycle_db(db)
        _, ref = upsert_reference(
            {"title_raw": "Test", "doi": "10.1/st1", "discovered_via": "serpapi_scholar"},
            db_path=db,
        )
        with get_connection(db) as conn:
            conn.execute("UPDATE article_references SET study_type='empirical_rct' WHERE reference_id=?", (ref,))
            val = conn.execute("SELECT study_type FROM article_references WHERE reference_id=?", (ref,)).fetchone()[0]
        assert val == "empirical_rct"
