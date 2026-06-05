"""
test_track2_task3.py — Isolated unit tests for Track 2, Task 3 pipeline.

Each test creates its own temporary SQLite database via tmp_path so there
is zero shared mutable state between test runs.  Running this file
repeatedly, or in parallel, will not produce DOI uniqueness errors or
stale PRISMA counts.

Run:
    py -3.14 -m pytest test_track2_task3.py -v
    py -3.14 -m pytest test_track2_task3.py -v -k test_prisma   # single test

These tests are fully offline (no network calls).
Live scraper tests live in test_track2_task3_live.py (requires SERP_API_KEY).
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# ── helpers ───────────────────────────────────────────────────────────────────

def _make_candidate(*, paper_id: str, doi: str = "", title: str = "Test paper",
                    gap_id: str = "GAP-PNU-001", year: int = 2022) -> dict:
    return {
        "paper_id": paper_id,
        "gap_id": gap_id,
        "scraper_source": "serpapi",
        "title": title,
        "doi": doi,
        "url": "https://example.com",
        "authors": ["Smith, J."],
        "year": year,
        "venue": "Test Journal",
        "cited_by": 5,
        "snippet": "Daylight improved sustained attention in office workers.",
    }


# ── article_db tests ──────────────────────────────────────────────────────────

class TestArticleDB:
    """Tests for article_db schema, upsert, and PRISMA counts.
    Each test receives a fresh isolated tmp_path — no shared state.
    """

    def test_init_creates_table(self, tmp_path):
        from article_db import init_db, get_connection
        db = str(tmp_path / "test.db")
        init_db(db)
        with get_connection(db) as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
        assert "article_references" in tables

    def test_upsert_inserts_new_row(self, tmp_path):
        from article_db import init_db, upsert_candidate, get_all_records
        db = str(tmp_path / "test.db")
        init_db(db)
        cand = _make_candidate(paper_id="P001", doi="10.1/test", title="Daylight and attention")
        inserted = upsert_candidate(cand, db_path=db)
        assert inserted is True
        rows = get_all_records(db)
        assert len(rows) == 1
        assert rows[0]["title"] == "Daylight and attention"
        assert rows[0]["doi"] == "10.1/test"

    def test_upsert_duplicate_paper_id_returns_false(self, tmp_path):
        from article_db import init_db, upsert_candidate
        db = str(tmp_path / "test.db")
        init_db(db)
        cand = _make_candidate(paper_id="P001")
        assert upsert_candidate(cand, db_path=db) is True
        assert upsert_candidate(cand, db_path=db) is False  # same paper_id → no insert

    def test_prisma_counts_empty_db(self, tmp_path):
        from article_db import init_db, get_prisma_counts
        db = str(tmp_path / "test.db")
        init_db(db)
        counts = get_prisma_counts(db)
        assert counts["identified"] == 0
        assert counts["accept"] == 0
        assert counts["pdfs_retrieved"] == 0

    def test_prisma_counts_reflect_decisions(self, tmp_path):
        from article_db import (init_db, upsert_candidate, update_stage1,
                                 update_stage2, set_final_decision, get_prisma_counts)
        db = str(tmp_path / "test.db")
        init_db(db)

        # Insert 3 candidates
        for i in range(1, 4):
            upsert_candidate(_make_candidate(
                paper_id=f"P00{i}", doi=f"10.1/t{i}", title=f"Paper {i}"
            ), db_path=db)

        # Stage 1 pass all
        for i in range(1, 4):
            update_stage1(f"P00{i}", "pass", db_path=db)

        # Stage 2: P001 = ACCEPT, P002 = REJECT, P003 = MISSING_ABSTRACT
        update_stage2("P001", status="ACCEPT",
                      abstract="Daylight improved attention in open-plan offices.",
                      abstract_source="semantic_scholar", db_path=db)
        set_final_decision("P001", "ACCEPT", db_path=db)

        update_stage2("P002", status="REJECT",
                      abstract="Unrelated paper about economics.",
                      abstract_source="crossref", db_path=db)
        set_final_decision("P002", "REJECT", db_path=db)

        update_stage2("P003", status="MISSING_ABSTRACT",
                      abstract="", abstract_source="none", db_path=db)
        set_final_decision("P003", "MISSING_ABSTRACT", db_path=db)

        counts = get_prisma_counts(db)
        assert counts["identified"] == 3
        assert counts["accept"] == 1
        assert counts["reject"] == 1
        assert counts["missing_abstract"] == 1
        assert counts["with_abstract"] == 2   # P001 + P002 have abstracts > 20 words each

    def test_get_pending_stage3_gate(self, tmp_path):
        """Stage 3 query must ONLY return ACCEPT and EDGE_CASE rows."""
        from article_db import (init_db, upsert_candidate, update_stage1,
                                 update_stage2, set_final_decision,
                                 get_pending_stage3)
        db = str(tmp_path / "test.db")
        init_db(db)

        pairs = [
            ("PA", "ACCEPT"),
            ("PE", "EDGE_CASE"),
            ("PR", "REJECT"),
            ("PM", "MISSING_ABSTRACT"),
        ]
        for pid, decision in pairs:
            upsert_candidate(_make_candidate(paper_id=pid, doi=f"10.1/{pid}"), db_path=db)
            update_stage1(pid, "pass", db_path=db)
            update_stage2(pid, status=decision, abstract="x" * 30,
                          abstract_source="test", db_path=db)
            set_final_decision(pid, decision, db_path=db)

        stage3_eligible = get_pending_stage3(db)
        eligible_ids = {r["paper_id"] for r in stage3_eligible}
        assert "PA" in eligible_ids
        assert "PE" in eligible_ids
        assert "PR" not in eligible_ids
        assert "PM" not in eligible_ids


# ── triage Stage 1 tests ──────────────────────────────────────────────────────

class TestTriageStage1:
    """Metadata-only filter — no network calls."""

    def test_stage1_passes_valid_record(self, tmp_path):
        from article_db import init_db, upsert_candidate
        import triage_engine
        db = str(tmp_path / "test.db")
        init_db(db)
        upsert_candidate(_make_candidate(
            paper_id="P001", title="Daylight and sustained attention", year=2022
        ), db_path=db)
        result = triage_engine.run_stage1(db)
        assert result["stage1_passed"] >= 1

    def test_stage1_rejects_short_title(self, tmp_path):
        from article_db import init_db, upsert_candidate, get_all_records
        import triage_engine
        db = str(tmp_path / "test.db")
        init_db(db)
        upsert_candidate(_make_candidate(
            paper_id="P001", title="Hi", year=2022
        ), db_path=db)
        triage_engine.run_stage1(db)
        rows = get_all_records(db)
        assert rows[0]["final_decision"] == "REJECT"

    def test_stage1_rejects_implausible_year(self, tmp_path):
        from article_db import init_db, upsert_candidate, get_all_records
        import triage_engine
        db = str(tmp_path / "test.db")
        init_db(db)
        upsert_candidate(_make_candidate(
            paper_id="P001", title="A valid title for this paper study", year=1880
        ), db_path=db)
        triage_engine.run_stage1(db)
        rows = get_all_records(db)
        assert rows[0]["final_decision"] == "REJECT"

    def test_stage1_rejects_allcaps_title(self, tmp_path):
        from article_db import init_db, upsert_candidate, get_all_records
        import triage_engine
        db = str(tmp_path / "test.db")
        init_db(db)
        upsert_candidate(_make_candidate(
            paper_id="P001", title="DAYLIGHT AND COGNITION IN OFFICES", year=2022
        ), db_path=db)
        triage_engine.run_stage1(db)
        rows = get_all_records(db)
        assert rows[0]["final_decision"] == "REJECT"


# ── triage Stage 2 duplicate detection test ───────────────────────────────────

class TestTriageStage2Dedup:
    """Duplicate DOI detection across two records."""

    def test_second_identical_doi_is_marked_duplicate(self, tmp_path):
        from article_db import init_db, upsert_candidate, update_stage1, get_all_records
        import triage_engine

        db = str(tmp_path / "test.db")
        init_db(db)

        # Two records with the same DOI, different paper_ids (from different gap queries)
        shared_doi = "10.1016/j.buildenv.2023.110241"
        for pid in ("P001", "P002"):
            upsert_candidate(_make_candidate(
                paper_id=pid, doi=shared_doi,
                title="Effect of daylight exposure on sustained attention"
            ), db_path=db)
            update_stage1(pid, "pass", db_path=db)

        # Mock the abstract fetch so stage 2 doesn't need network
        with patch.object(triage_engine, "_fetch_abstract",
                          return_value=("Daylight improved attention in open-plan office workers "
                                        "across three conditions in a randomized trial.",
                                        "semantic_scholar")):
            triage_engine.run_stage2(db)

        rows = {r["paper_id"]: r for r in get_all_records(db)}
        decisions = {rows["P001"]["final_decision"], rows["P002"]["final_decision"]}
        # One should be ACCEPT/EDGE_CASE, the other DUPLICATE
        assert "DUPLICATE" in decisions
        assert decisions != {"DUPLICATE", "DUPLICATE"}  # first one kept


# ── gap_extractor tests ───────────────────────────────────────────────────────

class TestGapExtractor:
    """Validate PNU gap generation and VOI scoring."""

    def test_produces_14_gaps(self):
        from gap_extractor import build_gaps_and_queries
        gaps, queries = build_gaps_and_queries()
        assert len(gaps) == 14
        assert len(queries) == 14

    def test_gaps_sorted_by_voi_descending(self):
        from gap_extractor import build_gaps_and_queries
        gaps, _ = build_gaps_and_queries()
        scores = [g["voi_score"] for g in gaps]
        assert scores == sorted(scores, reverse=True)

    def test_each_gap_has_unique_id(self):
        from gap_extractor import build_gaps_and_queries
        gaps, _ = build_gaps_and_queries()
        ids = [g["gap_id"] for g in gaps]
        assert len(ids) == len(set(ids))

    def test_each_query_has_boolean_and_ai_fields(self):
        from gap_extractor import build_gaps_and_queries
        _, queries = build_gaps_and_queries()
        for q in queries:
            assert "boolean_query" in q
            assert "ai_citation_query" in q
            assert q["boolean_query"].strip() != ""
            assert q["ai_citation_query"].strip() != ""

    def test_boolean_queries_contain_and_or(self):
        from gap_extractor import build_gaps_and_queries
        _, queries = build_gaps_and_queries()
        for q in queries:
            bq = q["boolean_query"].upper()
            assert " AND " in bq or " OR " in bq, (
                f"Boolean query for {q['gap_id']} has no AND/OR: {q['boolean_query']}"
            )

    def test_ai_queries_end_with_question_mark(self):
        from gap_extractor import build_gaps_and_queries
        _, queries = build_gaps_and_queries()
        for q in queries:
            assert q["ai_citation_query"].strip().endswith("?"), (
                f"AI query for {q['gap_id']} doesn't end with '?': {q['ai_citation_query'][:60]}"
            )


# ── harvest_layer unit tests ──────────────────────────────────────────────────

class TestHarvestLayer:
    """Scraper normalisation and DB write — SerpAPI is mocked."""

    MOCK_SERPAPI_RESPONSE = {
        "organic_results": [
            {
                "title": "Daylight exposure and sustained attention in open-plan offices",
                "link": "https://doi.org/10.1016/j.buildenv.2023.110241",
                "snippet": "Workers exposed to natural daylight showed higher sustained attention scores.",
                "publication_info": {
                    "summary": "A Smith, B Jones - Building and Environment, 2023",
                    "authors": [{"name": "A Smith"}, {"name": "B Jones"}],
                },
                "result_id": "abc123",
                "inline_links": {"cited_by": {"total": 42}},
            }
        ]
    }

    def test_scrape_serpapi_field_names(self):
        """Returned dicts must use 'snippet' not 'abstract'."""
        from harvest_layer import scrape_serpapi
        mock_resp = MagicMock()
        mock_resp.json.return_value = self.MOCK_SERPAPI_RESPONSE
        mock_resp.raise_for_status.return_value = None

        with patch("harvest_layer.requests.get", return_value=mock_resp):
            results = scrape_serpapi("daylight attention", num=1, gap_id="GAP-PNU-001")

        assert len(results) == 1
        r = results[0]
        assert "snippet" in r, "Field must be 'snippet', not 'abstract'"
        assert "abstract" not in r, "SerpAPI never returns full abstracts"
        assert r["scraper_source"] == "serpapi"
        assert r["doi"] == "10.1016/j.buildenv.2023.110241"
        assert r["gap_id"] == "GAP-PNU-001"
        assert r["year"] == 2023

    def test_scrape_serpapi_writes_to_db(self, tmp_path):
        from harvest_layer import scrape_serpapi
        from article_db import init_db, get_all_records, upsert_candidate
        db = str(tmp_path / "test.db")
        init_db(db)

        mock_resp = MagicMock()
        mock_resp.json.return_value = self.MOCK_SERPAPI_RESPONSE
        mock_resp.raise_for_status.return_value = None

        with patch("harvest_layer.requests.get", return_value=mock_resp):
            candidates = scrape_serpapi("test query", num=1, gap_id="GAP-PNU-001")

        for c in candidates:
            upsert_candidate(c, db_path=db)

        rows = get_all_records(db)
        assert len(rows) == 1
        assert rows[0]["scraper_source"] == "serpapi"
        assert rows[0]["doi"] == "10.1016/j.buildenv.2023.110241"
        # abstract column should be NULL at harvest time — Stage 2 fills it
        assert not rows[0]["abstract"]

    def test_scrape_serpapi_graceful_on_error(self):
        from harvest_layer import scrape_serpapi
        import requests as req
        with patch("harvest_layer.requests.get", side_effect=req.RequestException("timeout")):
            results = scrape_serpapi("any query", num=5)
        assert results == []

    def test_scholarly_gracefully_skips_when_not_installed(self):
        from harvest_layer import scrape_scholarly
        import builtins
        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "scholarly":
                raise ImportError("not installed")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            results = scrape_scholarly("daylight cognition", num=3)
        assert results == []

    def test_paper_scraper_gracefully_skips_when_not_installed(self):
        from harvest_layer import scrape_paper_scraper
        import builtins
        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name.startswith("paperscraper"):
                raise ImportError("not installed")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            results = scrape_paper_scraper("daylight cognition", num=3)
        assert results == []

    def test_scidownl_not_called_during_harvest(self):
        """acquire_pdf_scidownl must not be invoked by harvest_all_queries."""
        import harvest_layer
        with patch.object(harvest_layer, "acquire_pdf_scidownl") as mock_scidownl, \
             patch("harvest_layer.requests.get", return_value=MagicMock(
                 json=lambda: {"organic_results": []},
                 raise_for_status=lambda: None
             )):
            harvest_layer.harvest_all_queries(
                "query_results.json",
                db_path=":memory:",  # SQLite in-memory; no file isolation needed
                results_per_gap=1,
                delay=0,
                scrapers=["serpapi"],
                dry_run=True,
            )
        mock_scidownl.assert_not_called()


# ── PRISMA idempotency / repeated-run safety ──────────────────────────────────

class TestRepeatedRunSafety:
    """Running the pipeline twice must not crash or corrupt counts."""

    def test_repeated_upserts_do_not_duplicate(self, tmp_path):
        from article_db import init_db, upsert_candidate, get_prisma_counts
        db = str(tmp_path / "test.db")
        init_db(db)
        cand = _make_candidate(paper_id="P001", doi="10.1/test")

        # Insert same record 5 times
        results = [upsert_candidate(cand, db_path=db) for _ in range(5)]
        assert results[0] is True
        assert all(r is False for r in results[1:])

        counts = get_prisma_counts(db)
        assert counts["identified"] == 1

    def test_init_db_idempotent(self, tmp_path):
        from article_db import init_db
        db = str(tmp_path / "test.db")
        # Calling init_db twice must not raise
        init_db(db)
        init_db(db)


if __name__ == "__main__":
    # Convenience: run without pytest
    import subprocess, sys
    subprocess.run([sys.executable, "-m", "pytest", __file__, "-v"], check=True)
