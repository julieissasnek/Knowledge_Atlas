"""
test_phase6_prisma_dashboard.py -- Phase 6 PRISMA dashboard contract tests.

Coverage:
  DATA-A  Panel A: gap count matches query_results.json; top-5 sorted VOI DESC
  DATA-B  Panel B: query count = len(query_results.json); raw count = total rows
  DATA-C  Panel C: collected = rows with abstract > 50 chars; missing = abstract_missing rows
  DATA-D  Panel D: ACCEPT/EDGE_CASE/REJECT/MISSING live from phase4d_decision
  DATA-E  Panel E: screened = identified - duplicates; pdfs from papers table
  DATA-F  Panel F: null gaps = gaps in JSON with no rows in article_references
  CONSISTENCY  E.accept matches D.accept; E.identified = B.total_raw_results
  JSON    run_export writes valid JSON with all six panel keys
  HTML    generate_html contains all six panel IDs and no placeholder text
  REFRESH html contains prisma_dashboard.json fetch call
  EMPTY-DB  all panels return zero-filled dicts when DB is empty
  NULL-GAPS  gaps with no results listed in Panel F
  COVERED-GAPS  gaps with results NOT in Panel F null list
  STALE   generated_at is a valid ISO 8601 timestamp
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

TRACK2 = Path(__file__).resolve().parent

# ── Imports ────────────────────────────────────────────────────────────────────

from lifecycle_db import (
    get_connection,
    init_lifecycle_db,
    log_transition,
    upsert_reference,
    update_triage_stage,
    TRIAGE_STAGE_ABSTRACT_COLLECTED,
    TRIAGE_STAGE_ACCEPTED_FOR_DOWNLOAD,
)
from prisma_dashboard_data import (
    compute_panel_a,
    compute_panel_b,
    compute_panel_c,
    compute_panel_d,
    compute_panel_e,
    compute_panel_f,
    compute_prisma_dashboard_data,
)
from prisma_dashboard_export import (
    export_json,
    generate_html,
    run_export,
)

# ── Fixtures ───────────────────────────────────────────────────────────────────

_SAMPLE_QUERIES = [
    {"gap_id": "GAP-PNU-001",
     "boolean_query": "\"daylight\" AND \"attention\" AND \"office\"",
     "ai_citation_query": "How does daylight affect attention?"},
    {"gap_id": "GAP-PNU-002",
     "boolean_query": "\"biophilic design\" AND \"cognitive performance\"",
     "ai_citation_query": "Does biophilic design improve cognition?"},
    {"gap_id": "GAP-PNU-003",
     "boolean_query": "\"colour temperature\" AND \"working memory\"",
     "ai_citation_query": "How does CCT affect working memory?"},
]


def _write_queries(tmp_path: Path, queries=None) -> str:
    p = tmp_path / "queries.json"
    p.write_text(json.dumps(queries or _SAMPLE_QUERIES), encoding="utf-8")
    return str(p)


def _seed(
    db: str,
    *,
    gap_id: str = "GAP-PNU-001",
    doi: str = "10.1/test",
    decision: str = "ACCEPT",
    triage_stage: str = "accepted_for_download",
    voi_score: float = 0.80,
    abstract: str = "x" * 200,
    abstract_source: str = "semantic_scholar",
) -> str:
    init_lifecycle_db(db)
    _, ref_id = upsert_reference(
        {"title_raw": f"Paper {doi}", "doi": doi,
         "discovered_via": "serpapi_scholar",
         "gap_id": gap_id},
        db_path=db,
    )
    with get_connection(db) as conn:
        conn.execute(
            "UPDATE article_references "
            "SET triage_stage=?, phase4d_decision=?, phase4d_voi_score=?, "
            "    abstract=?, abstract_source=? "
            "WHERE reference_id=?",
            (triage_stage, decision, voi_score, abstract, abstract_source, ref_id),
        )
    return ref_id


# ===========================================================================
# Empty DB baseline
# ===========================================================================

class TestEmptyDB:
    def test_all_panels_zero_on_empty_db(self, tmp_path):
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        qf = _write_queries(tmp_path)
        data = compute_prisma_dashboard_data(db, qf)
        assert data["panel_b"]["total_raw_results"]    == 0
        assert data["panel_c"]["abstracts_collected"]  == 0
        assert data["panel_d"]["accept"]               == 0
        assert data["panel_e"]["identified"]           == 0
        assert data["panel_e"]["pdfs_acquired"]        == 0

    def test_panel_f_all_gaps_null_on_empty_db(self, tmp_path):
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        qf = _write_queries(tmp_path)
        pf = compute_panel_f(db, qf)
        assert pf["total_null_gaps"] == len(_SAMPLE_QUERIES)

    def test_panel_a_total_gaps_from_query_file(self, tmp_path):
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        qf = _write_queries(tmp_path)
        pa = compute_panel_a(db, qf)
        assert pa["total_gaps"] == len(_SAMPLE_QUERIES)


# ===========================================================================
# Panel A: Gap Summary
# ===========================================================================

class TestPanelA:
    def test_total_gaps_matches_query_file(self, tmp_path):
        db = str(tmp_path / "lc.db")
        _seed(db, gap_id="GAP-PNU-001")
        qf = _write_queries(tmp_path)
        pa = compute_panel_a(db, qf)
        assert pa["total_gaps"] == 3

    def test_gaps_with_results_counts_correctly(self, tmp_path):
        db = str(tmp_path / "lc.db")
        _seed(db, gap_id="GAP-PNU-001")
        qf = _write_queries(tmp_path)
        pa = compute_panel_a(db, qf)
        assert pa["gaps_with_results"] == 1

    def test_top5_sorted_by_voi_desc(self, tmp_path):
        db = str(tmp_path / "lc.db")
        _seed(db, gap_id="GAP-PNU-001", doi="10.1/a", voi_score=0.60)
        _seed(db, gap_id="GAP-PNU-002", doi="10.1/b", voi_score=0.90)
        _seed(db, gap_id="GAP-PNU-003", doi="10.1/c", voi_score=0.40)
        qf = _write_queries(tmp_path)
        pa = compute_panel_a(db, qf)
        scores = [g["max_voi_score"] for g in pa["top5_by_voi"]]
        assert scores == sorted(scores, reverse=True)

    def test_top5_capped_at_five(self, tmp_path):
        db = str(tmp_path / "lc.db")
        queries = [{"gap_id": f"GAP-PNU-{i:03d}", "boolean_query": f"q{i}"}
                   for i in range(1, 9)]
        qf = _write_queries(tmp_path, queries)
        for i, q in enumerate(queries):
            _seed(db, gap_id=q["gap_id"], doi=f"10.1/r{i}",
                  voi_score=round(0.5 + i * 0.05, 2))
        pa = compute_panel_a(db, qf)
        assert len(pa["top5_by_voi"]) <= 5

    def test_boolean_query_populated_from_file(self, tmp_path):
        db = str(tmp_path / "lc.db")
        _seed(db, gap_id="GAP-PNU-001")
        qf = _write_queries(tmp_path)
        pa = compute_panel_a(db, qf)
        entry = next((g for g in pa["top5_by_voi"] if g["gap_id"] == "GAP-PNU-001"), None)
        assert entry is not None
        assert "daylight" in entry["boolean_query"].lower()


# ===========================================================================
# Panel B: Search Summary
# ===========================================================================

class TestPanelB:
    def test_total_queries_matches_file(self, tmp_path):
        db = str(tmp_path / "lc.db")
        _seed(db)
        qf = _write_queries(tmp_path)
        pb = compute_panel_b(db, qf)
        assert pb["total_boolean_queries"] == 3

    def test_total_raw_results_matches_row_count(self, tmp_path):
        db = str(tmp_path / "lc.db")
        for i in range(5):
            _seed(db, doi=f"10.1/r{i}", gap_id="GAP-PNU-001")
        qf = _write_queries(tmp_path)
        pb = compute_panel_b(db, qf)
        assert pb["total_raw_results"] == 5

    def test_gaps_that_returned_results_correct(self, tmp_path):
        db = str(tmp_path / "lc.db")
        _seed(db, gap_id="GAP-PNU-001")
        _seed(db, gap_id="GAP-PNU-002", doi="10.1/b")
        qf = _write_queries(tmp_path)
        pb = compute_panel_b(db, qf)
        assert pb["gaps_that_returned_results"] == 2


# ===========================================================================
# Panel C: Abstract Telemetry
# ===========================================================================

class TestPanelC:
    def test_abstracts_collected_counts_long_abstracts(self, tmp_path):
        db = str(tmp_path / "lc.db")
        _seed(db, doi="10.1/long", abstract="x" * 200)   # collected
        _seed(db, doi="10.1/short", abstract="tiny")     # not collected
        qf = _write_queries(tmp_path)
        pc = compute_panel_c(db)
        assert pc["abstracts_collected"] == 1

    def test_missing_abstract_counts_abstract_missing_stage(self, tmp_path):
        db = str(tmp_path / "lc.db")
        ref = _seed(db, doi="10.1/miss", abstract="")
        with get_connection(db) as conn:
            conn.execute(
                "UPDATE article_references SET triage_stage='abstract_missing' "
                "WHERE reference_id=?", (ref,),
            )
        pc = compute_panel_c(db)
        assert pc["missing_abstract"] >= 1

    def test_source_breakdown_populated(self, tmp_path):
        db = str(tmp_path / "lc.db")
        _seed(db, doi="10.1/ss",  abstract_source="semantic_scholar", abstract="x"*200)
        _seed(db, doi="10.1/cr",  abstract_source="crossref",         abstract="y"*200)
        pc = compute_panel_c(db)
        assert "semantic_scholar" in pc["abstract_source_breakdown"]
        assert "crossref" in pc["abstract_source_breakdown"]


# ===========================================================================
# Panel D: Triage Results
# ===========================================================================

class TestPanelD:
    def test_accept_count_from_phase4d_decision(self, tmp_path):
        db = str(tmp_path / "lc.db")
        _seed(db, doi="10.1/a1", decision="ACCEPT")
        _seed(db, doi="10.1/a2", decision="ACCEPT")
        _seed(db, doi="10.1/ec", decision="EDGE_CASE",   triage_stage="edge_case_review")
        _seed(db, doi="10.1/rj", decision="REJECT",      triage_stage="rejected_at_abstract")
        pd_ = compute_panel_d(db)
        assert pd_["accept"]    == 2
        assert pd_["edge_case"] == 1
        assert pd_["reject"]    == 1

    def test_missing_abstract_decision_counted(self, tmp_path):
        db = str(tmp_path / "lc.db")
        _seed(db, doi="10.1/ma", decision="MISSING_ABSTRACT",
              triage_stage="rejected_at_abstract")
        pd_ = compute_panel_d(db)
        assert pd_["missing_abstract"] == 1

    def test_null_decision_not_counted(self, tmp_path):
        db = str(tmp_path / "lc.db")
        _seed(db, doi="10.1/nd", decision="", triage_stage="metadata_only")
        pd_ = compute_panel_d(db)
        assert sum(pd_.values()) == 0


# ===========================================================================
# Panel E: PRISMA Funnel
# ===========================================================================

class TestPanelE:
    def test_screened_equals_identified_minus_duplicates(self, tmp_path):
        db = str(tmp_path / "lc.db")
        _seed(db, doi="10.1/a")
        _seed(db, doi="10.1/b")
        ref_dup = _seed(db, doi="10.1/c")
        with get_connection(db) as conn:
            conn.execute(
                "UPDATE article_references SET triage_stage='duplicate' WHERE reference_id=?",
                (ref_dup,),
            )
        pe = compute_panel_e(db)
        assert pe["screened"] == pe["identified"] - pe["duplicates"]

    def test_pdfs_acquired_reads_papers_table(self, tmp_path):
        db = str(tmp_path / "lc.db")
        ref = _seed(db)
        with get_connection(db) as conn:
            conn.execute(
                "INSERT INTO papers (paper_id, reference_id, doi, local_pdf_path, "
                "acquisition_source, acquired_at) VALUES (?,?,?,?,?,datetime('now'))",
                ("PDF-TEST-000001", ref, "10.1/test", "/pdfs/test.pdf", "unpaywall"),
            )
        pe = compute_panel_e(db)
        assert pe["pdfs_acquired"] == 1

    def test_all_funnel_keys_present(self, tmp_path):
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        pe = compute_panel_e(db)
        required = {"identified", "duplicates", "metadata_rejected", "screened",
                    "abstracts_retrieved", "abstract_missing",
                    "accept", "edge_case", "reject", "pdfs_acquired", "unobtainable"}
        assert required.issubset(set(pe.keys()))


# ===========================================================================
# Panel F: Null Results Ledger
# ===========================================================================

class TestPanelF:
    def test_gap_with_no_rows_is_null(self, tmp_path):
        db = str(tmp_path / "lc.db")
        _seed(db, gap_id="GAP-PNU-001")   # has results
        # GAP-PNU-002 and GAP-PNU-003 have no rows
        qf = _write_queries(tmp_path)
        pf = compute_panel_f(db, qf)
        null_ids = {g["gap_id"] for g in pf["null_result_gaps"]}
        assert "GAP-PNU-001" not in null_ids
        assert "GAP-PNU-002" in null_ids
        assert "GAP-PNU-003" in null_ids

    def test_null_gap_includes_boolean_query(self, tmp_path):
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        qf = _write_queries(tmp_path)
        pf = compute_panel_f(db, qf)
        for g in pf["null_result_gaps"]:
            assert "boolean_query" in g
            assert len(g["boolean_query"]) > 0

    def test_covered_gaps_not_in_null_list(self, tmp_path):
        db = str(tmp_path / "lc.db")
        _seed(db, gap_id="GAP-PNU-002")
        qf = _write_queries(tmp_path)
        pf = compute_panel_f(db, qf)
        null_ids = {g["gap_id"] for g in pf["null_result_gaps"]}
        assert "GAP-PNU-002" not in null_ids
        assert "GAP-PNU-002" in pf["covered_gaps"]

    def test_total_null_count_correct(self, tmp_path):
        db = str(tmp_path / "lc.db")
        _seed(db, gap_id="GAP-PNU-001")
        qf = _write_queries(tmp_path)
        pf = compute_panel_f(db, qf)
        assert pf["total_null_gaps"] == len(pf["null_result_gaps"])


# ===========================================================================
# Cross-panel consistency
# ===========================================================================

class TestCrossPanel:
    def test_d_accept_equals_e_accept(self, tmp_path):
        db = str(tmp_path / "lc.db")
        for i in range(3):
            _seed(db, doi=f"10.1/r{i}", decision="ACCEPT")
        _seed(db, doi="10.1/rej", decision="REJECT", triage_stage="rejected_at_abstract")
        qf = _write_queries(tmp_path)
        data = compute_prisma_dashboard_data(db, qf)
        assert data["panel_d"]["accept"] == data["panel_e"]["accept"]

    def test_b_total_equals_e_identified(self, tmp_path):
        db = str(tmp_path / "lc.db")
        for i in range(4):
            _seed(db, doi=f"10.1/t{i}")
        qf = _write_queries(tmp_path)
        data = compute_prisma_dashboard_data(db, qf)
        assert data["panel_b"]["total_raw_results"] == data["panel_e"]["identified"]

    def test_all_six_panels_present(self, tmp_path):
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        qf = _write_queries(tmp_path)
        data = compute_prisma_dashboard_data(db, qf)
        for key in ("panel_a", "panel_b", "panel_c", "panel_d", "panel_e", "panel_f"):
            assert key in data

    def test_generated_at_is_iso8601(self, tmp_path):
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        qf = _write_queries(tmp_path)
        data = compute_prisma_dashboard_data(db, qf)
        ts = data["generated_at"]
        from datetime import datetime, timezone
        # Should parse without error
        datetime.fromisoformat(ts)


# ===========================================================================
# JSON export
# ===========================================================================

class TestJSONExport:
    def test_export_writes_valid_json(self, tmp_path):
        db  = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        qf  = _write_queries(tmp_path)
        out = str(tmp_path / "out.json")
        data = compute_prisma_dashboard_data(db, qf)
        export_json(data, out)
        loaded = json.loads(Path(out).read_text())
        assert "panel_a" in loaded

    def test_run_export_creates_json_file(self, tmp_path):
        db   = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        qf   = _write_queries(tmp_path)
        jout = str(tmp_path / "dash.json")
        hout = str(tmp_path / "dash.html")
        run_export(db_path=db, query_file=qf, json_out=jout, html_out=hout)
        assert Path(jout).exists()
        assert Path(hout).exists()


# ===========================================================================
# HTML generation
# ===========================================================================

class TestHTMLGeneration:
    def test_all_panel_ids_present(self, tmp_path):
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        qf = _write_queries(tmp_path)
        data = compute_prisma_dashboard_data(db, qf)
        html = generate_html(data)
        for panel_id in ("panel-a", "panel-b", "panel-c", "panel-d", "panel-e", "panel-f"):
            assert f'id="{panel_id}"' in html, f"Missing {panel_id}"

    def test_html_contains_live_note(self, tmp_path):
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        qf = _write_queries(tmp_path)
        data = compute_prisma_dashboard_data(db, qf)
        html = generate_html(data)
        assert "pipeline_lifecycle_full.db" in html

    def test_html_contains_refresh_fetch(self, tmp_path):
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        qf = _write_queries(tmp_path)
        data = compute_prisma_dashboard_data(db, qf)
        html = generate_html(data)
        assert "prisma_dashboard.json" in html
        assert "fetch(" in html

    def test_no_placeholder_text(self, tmp_path):
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        qf = _write_queries(tmp_path)
        data = compute_prisma_dashboard_data(db, qf)
        html = generate_html(data)
        for placeholder in ("PLACEHOLDER", "TODO", "FIXME", "hardcoded"):
            assert placeholder not in html

    def test_html_has_valid_structure(self, tmp_path):
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        qf = _write_queries(tmp_path)
        data = compute_prisma_dashboard_data(db, qf)
        html = generate_html(data)
        assert html.startswith("<!DOCTYPE html>")
        assert "</html>" in html

    def test_panel_e_contains_funnel_boxes(self, tmp_path):
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        qf = _write_queries(tmp_path)
        data = compute_prisma_dashboard_data(db, qf)
        html = generate_html(data)
        assert "funnel-box" in html
        assert "ACCEPT" in html
        assert "EDGE CASE" in html


# ===========================================================================
# Audit: Gemini defects fixed
# ===========================================================================

class TestGeminiDefectsFixed:
    """Verify that the six known gaps in prisma_export.py are closed."""

    def test_reads_lifecycle_db_not_article_db(self, tmp_path):
        """
        DEFECT 1: prisma_export.py read article_references.db.
        Fix: prisma_dashboard_data.py uses lifecycle_db.get_connection().
        """
        from prisma_dashboard_data import compute_prisma_dashboard_data
        import inspect
        src = inspect.getsource(compute_prisma_dashboard_data)
        # Sanity check: source doesn't import from article_db
        assert "from article_db" not in src
        assert "article_db" not in src

    def test_panel_f_null_ledger_exists(self, tmp_path):
        """DEFECT 6: Panel F was completely absent."""
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        qf = _write_queries(tmp_path)
        pf = compute_panel_f(db, qf)
        assert "null_result_gaps" in pf  # Panel F exists and has the right structure

    def test_panel_a_gap_voi_ranking_exists(self, tmp_path):
        """DEFECT 2: Panel A was absent."""
        db = str(tmp_path / "lc.db")
        _seed(db)
        qf = _write_queries(tmp_path)
        pa = compute_panel_a(db, qf)
        assert "top5_by_voi" in pa  # Panel A exists

    def test_panel_b_query_count_exists(self, tmp_path):
        """DEFECT 3: Panel B was absent."""
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        qf = _write_queries(tmp_path)
        pb = compute_panel_b(db, qf)
        assert "total_boolean_queries" in pb  # Panel B exists
        assert pb["total_boolean_queries"] == 3  # matches query_results.json

    def test_phase4d_decision_used_not_final_decision(self, tmp_path):
        """
        DEFECT 4/5: Gemini used final_decision (article_db schema).
        Fix: phase4d_decision (lifecycle_db schema) is used.
        """
        db = str(tmp_path / "lc.db")
        _seed(db, doi="10.1/a", decision="ACCEPT")
        pd_ = compute_panel_d(db)
        # If phase4d_decision is queried correctly, we get 1
        assert pd_["accept"] == 1

    def test_pdfs_from_papers_table_not_stage3_status(self, tmp_path):
        """
        DEFECT 5: Gemini used stage3_status='pdf_found' from article_db.
        Fix: COUNT(*) FROM papers table in lifecycle_db.
        """
        db = str(tmp_path / "lc.db")
        ref = _seed(db)
        with get_connection(db) as conn:
            conn.execute(
                "INSERT INTO papers (paper_id, reference_id, doi, local_pdf_path, "
                "acquisition_source, acquired_at) VALUES (?,?,?,?,?,datetime('now'))",
                ("PDF-TEST-000001", ref, "10.1/test", "/pdfs/t.pdf", "unpaywall"),
            )
        pe = compute_panel_e(db)
        assert pe["pdfs_acquired"] == 1  # from papers table, not stage3_status
