"""
test_prisma_funnel_table.py -- Phase 6B PRISMA funnel table contract tests.

Coverage:
  ROWS    All 10 funnel rows present in correct order
  SQL-1   Gaps targeted reads len(query_results.json)
  SQL-2   Queries executed counts distinct SerpAPI discovered_query
  SQL-3   Records returned = COUNT(*) FROM article_references
  SQL-4   Duplicates = COUNT WHERE triage_stage='duplicate'
  SQL-5   Abstracts = COUNT WHERE abstract > 50 chars AND valid source
  SQL-6   MISSING_ABSTRACT = abstract_missing stage OR phase4d_decision
  SQL-7   Screened = COUNT WHERE phase4d_decision IS NOT NULL
  SQL-8-10 ACCEPT / EDGE_CASE / REJECT from phase4d_decision
  LAW1-PASS  Law 1 delta = 0 for complete pipeline run
  LAW1-FAIL  Law 1 delta != 0 for incomplete run; alert emitted
  LAW1-EXPLAIN  Delta fully attributed to intermediate stages
  LAW2-PASS  Law 2 delta = 0 when all abstracts classified
  LAW2-FAIL  Law 2 delta != 0 when unclassified abstracts present
  LAW2-EXPLAIN  Delta attributed to abstract_collected stage
  STRICT  PipelineConservationError raised on unexplained residual
  NO-STRICT  No raise when delta explained by in-progress stages
  HTML   render_prisma_table_html contains all 10 rows + law badges
  HTML-PASS  PASS badge present when laws balance
  HTML-FAIL  ALERT badge + accordion present when laws don't balance
  EMPTY   Zero-filled result returned when DB absent
  INTEGRATE dashboard export includes prisma-funnel-table section
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

TRACK2 = Path(__file__).resolve().parent

from lifecycle_db import get_connection, init_lifecycle_db, upsert_reference
from generate_prisma_counts import (
    PipelineConservationError,
    VALID_ABSTRACT_SOURCES,
    compute_prisma_counts,
    print_audit_report,
    render_prisma_table_html,
)

# ── Fixtures ───────────────────────────────────────────────────────────────────

SAMPLE_QUERIES = [
    {"gap_id": "GAP-PNU-001", "boolean_query": "\"daylight\" AND \"attention\""},
    {"gap_id": "GAP-PNU-002", "boolean_query": "\"biophilic\" AND \"cognition\""},
    {"gap_id": "GAP-PNU-003", "boolean_query": "\"colour temperature\" AND \"memory\""},
]

LONG_ABSTRACT = "a" * 200  # length > 50, counts as collected


def _write_queries(tmp_path: Path, q=None) -> str:
    p = tmp_path / "q.json"
    p.write_text(json.dumps(q or SAMPLE_QUERIES), encoding="utf-8")
    return str(p)


def _seed(
    db: str,
    *,
    doi: str,
    gap_id: str = "GAP-PNU-001",
    discovered_via: str = "serpapi_scholar",
    triage_stage: str = "accepted_for_download",
    phase4d_decision: str = "ACCEPT",
    abstract: str = LONG_ABSTRACT,
    abstract_source: str = "semantic_scholar",
) -> str:
    _, ref = upsert_reference(
        {"title_raw": f"Paper {doi}", "doi": doi,
         "discovered_via": discovered_via, "gap_id": gap_id},
        db_path=db,
    )
    with get_connection(db) as conn:
        conn.execute(
            "UPDATE article_references "
            "SET triage_stage=?, phase4d_decision=?, abstract=?, abstract_source=? "
            "WHERE reference_id=?",
            (triage_stage, phase4d_decision, abstract, abstract_source, ref),
        )
    return ref


def _full_pipeline_db(tmp_path: Path) -> tuple[str, str]:
    """
    Seed a perfectly balanced pipeline state:
      5 records total
        1 duplicate (triage_stage='duplicate')
        1 abstract_missing
        3 classified: 1 ACCEPT + 1 EDGE_CASE + 1 REJECT (each with long abstract)

    Conservation law 1: 5 - 1 = 4 = 3 abstracts + 1 missing  -> PASS
    Conservation law 2: 3 abstracts = 1 + 1 + 1               -> PASS
    """
    db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
    qf = _write_queries(tmp_path)

    # Row 1: duplicate
    _seed(db, doi="10.1/dup", triage_stage="duplicate",
          phase4d_decision="", abstract="", abstract_source="")

    # Row 2: abstract_missing
    _seed(db, doi="10.1/miss", triage_stage="abstract_missing",
          phase4d_decision="", abstract="", abstract_source="")

    # Rows 3-5: classified with abstracts
    _seed(db, doi="10.1/acc", phase4d_decision="ACCEPT",
          abstract_source="semantic_scholar")
    _seed(db, doi="10.1/ec",  triage_stage="edge_case_review",
          phase4d_decision="EDGE_CASE", abstract_source="crossref")
    _seed(db, doi="10.1/rej", triage_stage="rejected_at_abstract",
          phase4d_decision="REJECT",    abstract_source="pubmed")

    return db, qf


# ===========================================================================
# Row counts
# ===========================================================================

class TestRowCounts:

    def test_10_rows_returned(self, tmp_path):
        db, qf = _full_pipeline_db(tmp_path)
        result = compute_prisma_counts(db, qf)
        assert len(result["rows"]) == 10

    def test_row_labels_in_order(self, tmp_path):
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        qf = _write_queries(tmp_path)
        result = compute_prisma_counts(db, qf)
        labels = [r["label"] for r in result["rows"]]
        assert "Gaps targeted" in labels[0]
        assert "Queries executed" in labels[1]
        assert "Records returned" in labels[2]
        assert "Duplicates" in labels[3]
        assert "Abstracts collected" in labels[4]
        assert "MISSING_ABSTRACT" in labels[5]
        assert "Screened" in labels[6]
        assert "ACCEPT" in labels[7]
        assert "EDGE_CASE" in labels[8]
        assert "REJECT" in labels[9]

    def test_row_source_sql_non_empty(self, tmp_path):
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        qf = _write_queries(tmp_path)
        result = compute_prisma_counts(db, qf)
        for r in result["rows"]:
            assert len(r["source_sql"]) > 0, f"Empty source_sql for {r['label']}"


# ===========================================================================
# SQL-1: Gaps targeted
# ===========================================================================

class TestSQL1GapsTargeted:
    def test_gaps_count_matches_query_file(self, tmp_path):
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        qf = _write_queries(tmp_path)   # 3 gaps
        result = compute_prisma_counts(db, qf)
        assert result["rows"][0]["count"] == 3

    def test_gaps_from_file_not_db(self, tmp_path):
        """Gaps targeted comes from the file, not from rows in the DB."""
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        qf = _write_queries(tmp_path, [{"gap_id": "GAP-001", "boolean_query": "x"}])
        result = compute_prisma_counts(db, qf)
        assert result["rows"][0]["count"] == 1


# ===========================================================================
# SQL-2: Queries executed (SerpAPI)
# ===========================================================================

class TestSQL2QueriesExecuted:
    def test_counts_distinct_serpapi_gap_ids(self, tmp_path):
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        qf = _write_queries(tmp_path)
        # Two different gaps via SerpAPI, three rows total
        _seed(db, doi="10.1/a", gap_id="GAP-PNU-001", discovered_via="serpapi_scholar")
        _seed(db, doi="10.1/b", gap_id="GAP-PNU-001", discovered_via="serpapi_scholar")
        _seed(db, doi="10.1/c", gap_id="GAP-PNU-002", discovered_via="serpapi_scholar")
        result = compute_prisma_counts(db, qf)
        assert result["rows"][1]["count"] == 2   # 2 distinct gap_ids

    def test_non_serpapi_rows_excluded(self, tmp_path):
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        qf = _write_queries(tmp_path)
        _seed(db, doi="10.1/s", gap_id="GAP-PNU-001", discovered_via="scholarly_search")
        result = compute_prisma_counts(db, qf)
        assert result["rows"][1]["count"] == 0


# ===========================================================================
# SQL-3: Records returned
# ===========================================================================

class TestSQL3RecordsReturned:
    def test_all_rows_counted(self, tmp_path):
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        qf = _write_queries(tmp_path)
        for i in range(7):
            _seed(db, doi=f"10.1/r{i}")
        result = compute_prisma_counts(db, qf)
        assert result["rows"][2]["count"] == 7


# ===========================================================================
# SQL-4: Duplicates removed
# ===========================================================================

class TestSQL4Duplicates:
    def test_only_duplicate_stage_counted(self, tmp_path):
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        qf = _write_queries(tmp_path)
        _seed(db, doi="10.1/dup", triage_stage="duplicate",
              phase4d_decision="", abstract="", abstract_source="")
        _seed(db, doi="10.1/ok")
        result = compute_prisma_counts(db, qf)
        assert result["rows"][3]["count"] == 1


# ===========================================================================
# SQL-5: Abstracts collected
# ===========================================================================

class TestSQL5AbstractsCollected:
    def test_long_abstract_with_valid_source_counted(self, tmp_path):
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        qf = _write_queries(tmp_path)
        _seed(db, doi="10.1/ok", abstract=LONG_ABSTRACT, abstract_source="semantic_scholar")
        result = compute_prisma_counts(db, qf)
        assert result["rows"][4]["count"] == 1

    def test_snippet_source_not_counted(self, tmp_path):
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        qf = _write_queries(tmp_path)
        _seed(db, doi="10.1/snip", abstract=LONG_ABSTRACT, abstract_source="snippet")
        result = compute_prisma_counts(db, qf)
        assert result["rows"][4]["count"] == 0

    def test_short_abstract_not_counted(self, tmp_path):
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        qf = _write_queries(tmp_path)
        _seed(db, doi="10.1/short", abstract="short", abstract_source="crossref")
        result = compute_prisma_counts(db, qf)
        assert result["rows"][4]["count"] == 0

    def test_all_valid_sources_counted(self, tmp_path):
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        qf = _write_queries(tmp_path)
        for i, src in enumerate(VALID_ABSTRACT_SOURCES):
            _seed(db, doi=f"10.1/s{i}", abstract=LONG_ABSTRACT, abstract_source=src)
        result = compute_prisma_counts(db, qf)
        assert result["rows"][4]["count"] == len(VALID_ABSTRACT_SOURCES)


# ===========================================================================
# SQL-6: MISSING_ABSTRACT
# ===========================================================================

class TestSQL6MissingAbstract:
    def test_abstract_missing_stage_counted(self, tmp_path):
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        qf = _write_queries(tmp_path)
        _seed(db, doi="10.1/m", triage_stage="abstract_missing",
              phase4d_decision="", abstract="", abstract_source="")
        result = compute_prisma_counts(db, qf)
        assert result["rows"][5]["count"] == 1

    def test_phase4d_missing_abstract_counted(self, tmp_path):
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        qf = _write_queries(tmp_path)
        _seed(db, doi="10.1/p4dm",
              triage_stage="rejected_at_abstract",
              phase4d_decision="MISSING_ABSTRACT",
              abstract="", abstract_source="")
        result = compute_prisma_counts(db, qf)
        assert result["rows"][5]["count"] == 1

    def test_no_double_count_when_both_conditions_true(self, tmp_path):
        """A row matching both conditions must be counted once."""
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        qf = _write_queries(tmp_path)
        _seed(db, doi="10.1/both",
              triage_stage="abstract_missing",
              phase4d_decision="MISSING_ABSTRACT",
              abstract="", abstract_source="")
        result = compute_prisma_counts(db, qf)
        assert result["rows"][5]["count"] == 1


# ===========================================================================
# SQL-7-10: Classifier counts
# ===========================================================================

class TestSQL7to10Classifier:
    def test_screened_counts_all_phase4d_classified(self, tmp_path):
        db, qf = _full_pipeline_db(tmp_path)
        result = compute_prisma_counts(db, qf)
        assert result["rows"][6]["count"] == 3  # ACCEPT + EDGE + REJECT

    def test_accept_count(self, tmp_path):
        db, qf = _full_pipeline_db(tmp_path)
        result = compute_prisma_counts(db, qf)
        assert result["rows"][7]["count"] == 1

    def test_edge_case_count(self, tmp_path):
        db, qf = _full_pipeline_db(tmp_path)
        result = compute_prisma_counts(db, qf)
        assert result["rows"][8]["count"] == 1

    def test_reject_count(self, tmp_path):
        db, qf = _full_pipeline_db(tmp_path)
        result = compute_prisma_counts(db, qf)
        assert result["rows"][9]["count"] == 1

    def test_screened_equals_sum_of_decisions(self, tmp_path):
        db, qf = _full_pipeline_db(tmp_path)
        result = compute_prisma_counts(db, qf)
        r = result["rows"]
        assert r[6]["count"] == r[7]["count"] + r[8]["count"] + r[9]["count"]


# ===========================================================================
# Conservation Law 1
# ===========================================================================

class TestConservationLaw1:
    def test_law1_passes_for_complete_run(self, tmp_path):
        db, qf = _full_pipeline_db(tmp_path)
        result = compute_prisma_counts(db, qf)
        assert result["conservation"]["law1_delta"] == 0
        assert result["conservation"]["laws_balance"] == (
            result["conservation"]["law1_delta"] == 0 and
            result["conservation"]["law2_delta"] == 0
        )

    def test_law1_alert_for_incomplete_run(self, tmp_path):
        """Rows in metadata_only / abstract_pending cause non-zero Law 1 delta."""
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        qf = _write_queries(tmp_path)
        # 3 rows: 1 abstract, 1 missing, 1 still in metadata_only (pipeline incomplete)
        _seed(db, doi="10.1/a", abstract=LONG_ABSTRACT, abstract_source="crossref")
        _seed(db, doi="10.1/m", triage_stage="abstract_missing",
              phase4d_decision="", abstract="", abstract_source="")
        _seed(db, doi="10.1/p", triage_stage="metadata_only",
              phase4d_decision="", abstract="", abstract_source="")

        result = compute_prisma_counts(db, qf)
        # Law 1: (3-0) != (1 + 1) => delta = 1
        assert result["conservation"]["law1_delta"] == 1

    def test_law1_delta_explained_by_intermediate_stages(self, tmp_path):
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        qf = _write_queries(tmp_path)
        _seed(db, doi="10.1/a", abstract=LONG_ABSTRACT, abstract_source="crossref")
        _seed(db, doi="10.1/p", triage_stage="metadata_only",
              phase4d_decision="", abstract="", abstract_source="")

        result = compute_prisma_counts(db, qf)
        ad = result["audit_delta"]
        # delta = 1; explained by metadata_only = 1; unexplained = 0
        assert ad["law1_unexplained_residual"] == 0
        assert ad["law1_explained_by"]["metadata_only"] == 1


# ===========================================================================
# Conservation Law 2
# ===========================================================================

class TestConservationLaw2:
    def test_law2_passes_for_complete_run(self, tmp_path):
        db, qf = _full_pipeline_db(tmp_path)
        result = compute_prisma_counts(db, qf)
        assert result["conservation"]["law2_delta"] == 0

    def test_law2_alert_when_abstracts_unclassified(self, tmp_path):
        """Rows in abstract_collected (awaiting Phase 4D) cause non-zero Law 2 delta."""
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        qf = _write_queries(tmp_path)
        # 1 classified ACCEPT + 1 abstract_collected but not yet Phase 4D'd
        _seed(db, doi="10.1/acc", abstract=LONG_ABSTRACT, abstract_source="semantic_scholar")
        _seed(db, doi="10.1/pending",
              triage_stage="abstract_collected",
              phase4d_decision="",
              abstract=LONG_ABSTRACT,
              abstract_source="pubmed")

        result = compute_prisma_counts(db, qf)
        # Law 2: 2 abstracts != 1 (ACCEPT only) => delta = 1
        assert result["conservation"]["law2_delta"] == 1

    def test_law2_delta_explained_by_abstract_collected_stage(self, tmp_path):
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        qf = _write_queries(tmp_path)
        _seed(db, doi="10.1/acc")
        _seed(db, doi="10.1/pend",
              triage_stage="abstract_collected",
              phase4d_decision="",
              abstract=LONG_ABSTRACT,
              abstract_source="openalex")

        result = compute_prisma_counts(db, qf)
        ad = result["audit_delta"]
        assert ad["law2_unexplained_residual"] == 0
        assert ad["law2_explained_by"]["abstract_collected_not_classified"] == 1


# ===========================================================================
# Strict mode
# ===========================================================================

class TestStrictMode:
    def test_no_raise_when_laws_balance(self, tmp_path):
        db, qf = _full_pipeline_db(tmp_path)
        result = compute_prisma_counts(db, qf, strict=True)
        assert result["conservation"]["laws_balance"] is True

    def test_no_raise_when_delta_explained_by_pipeline(self, tmp_path):
        """In-progress delta is explained -- strict mode must NOT raise."""
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        qf = _write_queries(tmp_path)
        _seed(db, doi="10.1/a")  # ACCEPT, classified
        _seed(db, doi="10.1/p",  # still in metadata_only
              triage_stage="metadata_only",
              phase4d_decision="", abstract="", abstract_source="")

        # Law 1 delta = 1, but explained by metadata_only -> should NOT raise
        result = compute_prisma_counts(db, qf, strict=True)
        assert result["audit_delta"]["law1_unexplained_residual"] == 0

    def test_raises_on_unexplained_residual(self, tmp_path):
        """Seed a DB where counts don't add up even accounting for all stages."""
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        qf = _write_queries(tmp_path)
        # Plant 3 ACCEPT rows but only 1 abstract -- Law 2 delta = -2 unexplained
        for i in range(3):
            _seed(db, doi=f"10.1/acc{i}", phase4d_decision="ACCEPT",
                  abstract="" if i > 0 else LONG_ABSTRACT,
                  abstract_source="" if i > 0 else "semantic_scholar")

        with pytest.raises(PipelineConservationError) as exc_info:
            compute_prisma_counts(db, qf, strict=True)

        assert exc_info.value.law2_delta != 0


# ===========================================================================
# HTML rendering
# ===========================================================================

class TestHTMLRendering:
    def test_html_contains_all_row_labels(self, tmp_path):
        db, qf = _full_pipeline_db(tmp_path)
        result = compute_prisma_counts(db, qf)
        html = render_prisma_table_html(result)
        for label_fragment in (
            "Gaps targeted", "Queries executed", "Records returned",
            "Duplicates", "Abstracts collected", "MISSING_ABSTRACT",
            "Screened", "ACCEPT", "EDGE_CASE", "REJECT",
        ):
            assert label_fragment in html, f"Missing label: {label_fragment}"

    def test_html_shows_pass_badge_when_balanced(self, tmp_path):
        db, qf = _full_pipeline_db(tmp_path)
        result = compute_prisma_counts(db, qf)
        html = render_prisma_table_html(result)
        assert "PASS" in html
        assert "ALERT" not in html

    def test_html_shows_alert_badge_and_accordion_when_unbalanced(self, tmp_path):
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        qf = _write_queries(tmp_path)
        _seed(db, doi="10.1/a")
        _seed(db, doi="10.1/p",
              triage_stage="metadata_only",
              phase4d_decision="", abstract="", abstract_source="")
        result = compute_prisma_counts(db, qf)
        html = render_prisma_table_html(result)
        assert "ALERT" in html
        assert "audit-details" in html

    def test_html_section_id_present(self, tmp_path):
        db, qf = _full_pipeline_db(tmp_path)
        result = compute_prisma_counts(db, qf)
        html = render_prisma_table_html(result)
        assert 'id="prisma-funnel-table"' in html

    def test_html_contains_no_hardcoded_zero_placeholders(self, tmp_path):
        """Verify the HTML renders actual counts, not template placeholders."""
        db, qf = _full_pipeline_db(tmp_path)
        result = compute_prisma_counts(db, qf)
        html = render_prisma_table_html(result)
        assert "PLACEHOLDER" not in html
        assert "TODO" not in html


# ===========================================================================
# Empty DB
# ===========================================================================

class TestEmptyDB:
    def test_zero_filled_result_on_empty_db(self, tmp_path):
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        qf = _write_queries(tmp_path)
        result = compute_prisma_counts(db, qf)
        for r in result["rows"][1:]:   # skip gaps_targeted (comes from file)
            assert r["count"] == 0, f"Non-zero on empty DB: {r['label']} = {r['count']}"

    def test_laws_balance_on_empty_db(self, tmp_path):
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        qf = _write_queries(tmp_path)
        result = compute_prisma_counts(db, qf)
        assert result["conservation"]["law1_delta"] == 0
        assert result["conservation"]["law2_delta"] == 0


# ===========================================================================
# Dashboard integration
# ===========================================================================

class TestDashboardIntegration:
    def test_export_includes_prisma_funnel_table(self, tmp_path):
        from prisma_dashboard_export import run_export
        db = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        qf = _write_queries(tmp_path)
        jout = str(tmp_path / "dash.json")
        hout = str(tmp_path / "dash.html")
        run_export(db_path=db, query_file=qf, json_out=jout, html_out=hout)
        html = Path(hout).read_text(encoding="utf-8")
        assert 'id="prisma-funnel-table"' in html

    def test_audit_report_prints_without_error(self, tmp_path):
        import io
        db, qf = _full_pipeline_db(tmp_path)
        result = compute_prisma_counts(db, qf)
        buf = io.StringIO()
        print_audit_report(result, file=buf)
        out = buf.getvalue()
        assert "PASS" in out
        assert "PRISMA FUNNEL TABLE" in out
