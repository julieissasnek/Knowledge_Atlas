"""
test_lifecycle_dry_run.py — Unit tests for lifecycle_db.py (Phase 3 coordinator).

No network calls, no shared state.  Every test uses pytest's tmp_path fixture
to create a fresh isolated SQLite database.

Covers:
  - Schema initialisation (init_lifecycle_db)
  - normalize_doi() canonical contract
  - normalize_title() helper
  - reference_id format  (REF-YYYY-MM-DD-NNNNNN)
  - Successful insert → UpsertResult.INSERTED
  - DOI-merge dedup → UpsertResult.DOI_MERGE, discovered_via appended
  - DOI-merge: same discovered_via not duplicated in the appended string
  - No-DOI fuzzy title dedup → UpsertResult.DUPLICATE (via pdf_identity_inventory)
  - All 7 discovered_via enum values insert successfully
  - Invalid discovered_via raises ValueError
  - write_candidates_to_lifecycle() batch bridge (happy path + skipped + error)
  - dry_run mode returns predicted outcome without writing
  - get_lifecycle_counts() aggregates triage_stage correctly
  - init_lifecycle_db() is idempotent (safe to call twice)

Run:
    py -3.14 -m pytest test_lifecycle_dry_run.py -v
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db(tmp_path) -> str:
    """Return a fresh per-test DB path (not yet created)."""
    return str(tmp_path / "lifecycle_test.db")


def _init(db: str):
    from lifecycle_db import init_lifecycle_db
    init_lifecycle_db(db)


def _base_record(**overrides) -> dict:
    """Minimal valid record for upsert_reference()."""
    r = {
        "title_raw":        "Daylight exposure and sustained attention in open-plan offices",
        "doi":              "10.1016/j.buildenv.2023.110241",
        "authors":          ["Smith, A.", "Jones, B."],
        "publication_year": 2023,
        "venue":            "Building and Environment",
        "discovered_via":   "serpapi_scholar",
        "discovered_query": "GAP-PNU-001",
        "discovery_run_id": "RUN-001",
        "raw_citation":     "",
        "snippet":          "Workers exposed to daylight showed higher attention scores.",
    }
    r.update(overrides)
    return r


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class TestSchema:
    def test_init_creates_article_references_table(self, tmp_path):
        db = _db(tmp_path)
        _init(db)
        from lifecycle_db import get_connection
        with get_connection(db) as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
        assert "article_references" in tables

    def test_init_creates_pdf_identity_inventory_table(self, tmp_path):
        db = _db(tmp_path)
        _init(db)
        from lifecycle_db import get_connection
        with get_connection(db) as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
        assert "pdf_identity_inventory" in tables

    def test_init_idempotent(self, tmp_path):
        """Calling init_lifecycle_db twice must not raise."""
        db = _db(tmp_path)
        _init(db)
        _init(db)  # second call — must be safe


# ---------------------------------------------------------------------------
# normalize_doi
# ---------------------------------------------------------------------------

class TestNormalizeDoi:
    def test_strips_https_prefix(self):
        from lifecycle_db import normalize_doi
        assert normalize_doi("https://doi.org/10.1016/J.BUILDENV.2023.110241") \
               == "10.1016/j.buildenv.2023.110241"

    def test_strips_http_dx_prefix(self):
        from lifecycle_db import normalize_doi
        assert normalize_doi("http://dx.doi.org/10.1093/BRAIN/AWZ123") \
               == "10.1093/brain/awz123"

    def test_strips_doi_label(self):
        from lifecycle_db import normalize_doi
        assert normalize_doi("DOI: 10.1093/brain/awz123") == "10.1093/brain/awz123"
        assert normalize_doi("doi:10.1093/brain/awz123")  == "10.1093/brain/awz123"

    def test_lowercases(self):
        from lifecycle_db import normalize_doi
        assert normalize_doi("10.1016/J.BUILDENV.2023.110241") \
               == "10.1016/j.buildenv.2023.110241"

    def test_empty_returns_empty(self):
        from lifecycle_db import normalize_doi
        assert normalize_doi("") == ""
        assert normalize_doi(None) == ""  # type: ignore[arg-type]

    def test_plain_doi_unchanged_except_case(self):
        from lifecycle_db import normalize_doi
        assert normalize_doi("10.1234/test.2023") == "10.1234/test.2023"


# ---------------------------------------------------------------------------
# normalize_title
# ---------------------------------------------------------------------------

class TestNormalizeTitle:
    def test_lowercases_and_strips_punctuation(self):
        from lifecycle_db import normalize_title
        result = normalize_title("Daylight, Cognition & Offices!")
        # Punctuation replaced by space then whitespace collapsed to single space
        assert result == "daylight cognition offices"

    def test_collapses_whitespace(self):
        from lifecycle_db import normalize_title
        # normalize_title collapses multiple spaces to one (re.sub r"\s+" → " ")
        assert normalize_title("  a   b   c  ") == "a b c"

    def test_empty_returns_empty(self):
        from lifecycle_db import normalize_title
        assert normalize_title("") == ""


# ---------------------------------------------------------------------------
# reference_id format
# ---------------------------------------------------------------------------

class TestReferenceIdFormat:
    REF_PATTERN = re.compile(r"^REF-\d{4}-\d{2}-\d{2}-\d{6}$")

    def test_reference_id_matches_format(self, tmp_path):
        db = _db(tmp_path)
        _init(db)
        from lifecycle_db import upsert_reference
        _, ref_id = upsert_reference(_base_record(), db_path=db)
        assert self.REF_PATTERN.match(ref_id), f"Bad reference_id format: {ref_id}"

    def test_reference_id_date_is_today_utc(self, tmp_path):
        db = _db(tmp_path)
        _init(db)
        from lifecycle_db import upsert_reference
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        _, ref_id = upsert_reference(_base_record(), db_path=db)
        assert ref_id.startswith(f"REF-{today}-"), \
            f"reference_id date portion should be {today}, got {ref_id}"

    def test_sequence_increments_per_day(self, tmp_path):
        db = _db(tmp_path)
        _init(db)
        from lifecycle_db import upsert_reference
        _, id1 = upsert_reference(_base_record(doi="10.1/a"), db_path=db)
        _, id2 = upsert_reference(_base_record(doi="10.1/b"), db_path=db)
        seq1 = int(id1.split("-")[-1])
        seq2 = int(id2.split("-")[-1])
        assert seq2 == seq1 + 1


# ---------------------------------------------------------------------------
# Upsert — happy path
# ---------------------------------------------------------------------------

class TestUpsertInserted:
    def test_new_record_returns_inserted(self, tmp_path):
        db = _db(tmp_path)
        _init(db)
        from lifecycle_db import upsert_reference, UpsertResult
        result, ref_id = upsert_reference(_base_record(), db_path=db)
        assert result == UpsertResult.INSERTED
        assert ref_id.startswith("REF-")

    def test_record_stored_correctly(self, tmp_path):
        db = _db(tmp_path)
        _init(db)
        from lifecycle_db import upsert_reference, get_connection
        upsert_reference(_base_record(), db_path=db)
        with get_connection(db) as conn:
            row = conn.execute(
                "SELECT * FROM article_references"
            ).fetchone()
        assert row["doi"] == "10.1016/j.buildenv.2023.110241"
        assert row["triage_stage"] == "metadata_only"
        assert row["discovered_via"] == "serpapi_scholar"

    def test_default_triage_stage_is_metadata_only(self, tmp_path):
        db = _db(tmp_path)
        _init(db)
        from lifecycle_db import upsert_reference, get_connection
        upsert_reference(_base_record(), db_path=db)
        with get_connection(db) as conn:
            row = conn.execute("SELECT triage_stage FROM article_references").fetchone()
        assert row["triage_stage"] == "metadata_only"


# ---------------------------------------------------------------------------
# DOI-merge deduplication
# ---------------------------------------------------------------------------

class TestDoiMerge:
    def test_same_doi_returns_doi_merge(self, tmp_path):
        db = _db(tmp_path)
        _init(db)
        from lifecycle_db import upsert_reference, UpsertResult
        upsert_reference(_base_record(), db_path=db)
        # Same DOI, different discovered_via
        result, ref_id = upsert_reference(
            _base_record(discovered_via="crossref_search"), db_path=db
        )
        assert result == UpsertResult.DOI_MERGE

    def test_doi_merge_appends_discovered_via(self, tmp_path):
        db = _db(tmp_path)
        _init(db)
        from lifecycle_db import upsert_reference, get_connection
        _, ref_id = upsert_reference(_base_record(), db_path=db)
        upsert_reference(_base_record(discovered_via="crossref_search"), db_path=db)
        with get_connection(db) as conn:
            row = conn.execute(
                "SELECT discovered_via FROM article_references WHERE reference_id = ?",
                (ref_id,)
            ).fetchone()
        assert "serpapi_scholar" in row["discovered_via"]
        assert "crossref_search" in row["discovered_via"]

    def test_doi_merge_does_not_create_new_row(self, tmp_path):
        db = _db(tmp_path)
        _init(db)
        from lifecycle_db import upsert_reference, get_connection
        upsert_reference(_base_record(), db_path=db)
        upsert_reference(_base_record(discovered_via="crossref_search"), db_path=db)
        with get_connection(db) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM article_references"
            ).fetchone()[0]
        assert count == 1

    def test_doi_merge_same_source_not_duplicated_in_string(self, tmp_path):
        """Inserting same discovered_via twice must not produce 'serpapi_scholar, serpapi_scholar'."""
        db = _db(tmp_path)
        _init(db)
        from lifecycle_db import upsert_reference, get_connection
        _, ref_id = upsert_reference(_base_record(), db_path=db)
        # Same DOI, same discovered_via
        upsert_reference(_base_record(), db_path=db)
        with get_connection(db) as conn:
            row = conn.execute(
                "SELECT discovered_via FROM article_references WHERE reference_id = ?",
                (ref_id,)
            ).fetchone()
        # "serpapi_scholar" should appear exactly once
        parts = [p.strip() for p in row["discovered_via"].split(",")]
        assert parts.count("serpapi_scholar") == 1

    def test_doi_normalisation_before_match(self, tmp_path):
        """URL-prefixed DOI must match a previously inserted bare DOI."""
        db = _db(tmp_path)
        _init(db)
        from lifecycle_db import upsert_reference, UpsertResult
        upsert_reference(_base_record(doi="10.1016/j.buildenv.2023.110241"), db_path=db)
        result, _ = upsert_reference(
            _base_record(
                doi="https://doi.org/10.1016/J.BUILDENV.2023.110241",
                discovered_via="crossref_search",
            ),
            db_path=db,
        )
        assert result == UpsertResult.DOI_MERGE


# ---------------------------------------------------------------------------
# Fuzzy title dedup (via pdf_identity_inventory)
# ---------------------------------------------------------------------------

class TestFuzzyTitleDedup:
    def _seed_inventory(self, db: str, title: str) -> None:
        """Insert a row directly into pdf_identity_inventory."""
        from lifecycle_db import get_connection, normalize_title
        from datetime import datetime, timezone
        with get_connection(db) as conn:
            conn.execute(
                "INSERT INTO pdf_identity_inventory (reference_id, title_normalized, ingested_at) "
                "VALUES (?, ?, ?)",
                ("EXISTING-001", normalize_title(title), datetime.now(timezone.utc).isoformat()),
            )

    def test_high_similarity_title_returns_duplicate(self, tmp_path):
        db = _db(tmp_path)
        _init(db)
        from lifecycle_db import upsert_reference, UpsertResult
        self._seed_inventory(
            db, "Daylight exposure and sustained attention in open-plan offices"
        )
        # Insert a record with no DOI so fuzzy check is triggered
        result, _ = upsert_reference(
            _base_record(
                doi="",
                # Nearly identical title — should match above threshold 0.85
                title_raw="Daylight exposure and sustained attention in open-plan offices",
            ),
            db_path=db,
        )
        assert result == UpsertResult.DUPLICATE

    def test_duplicate_triage_stage_set_correctly(self, tmp_path):
        db = _db(tmp_path)
        _init(db)
        from lifecycle_db import upsert_reference, get_connection
        self._seed_inventory(
            db, "Daylight exposure and sustained attention in open-plan offices"
        )
        _, ref_id = upsert_reference(
            _base_record(
                doi="",
                title_raw="Daylight exposure and sustained attention in open-plan offices",
            ),
            db_path=db,
        )
        with get_connection(db) as conn:
            row = conn.execute(
                "SELECT triage_stage FROM article_references WHERE reference_id = ?",
                (ref_id,),
            ).fetchone()
        assert row["triage_stage"] == "duplicate"

    def test_low_similarity_title_returns_inserted(self, tmp_path):
        db = _db(tmp_path)
        _init(db)
        from lifecycle_db import upsert_reference, UpsertResult
        self._seed_inventory(db, "Unrelated paper about quantum computing")
        result, _ = upsert_reference(
            _base_record(
                doi="",
                title_raw="Effects of natural light on cognitive performance in office environments",
            ),
            db_path=db,
        )
        assert result == UpsertResult.INSERTED

    def test_empty_inventory_always_inserts(self, tmp_path):
        """pdf_identity_inventory is empty → no fuzzy match possible → INSERTED."""
        db = _db(tmp_path)
        _init(db)
        from lifecycle_db import upsert_reference, UpsertResult
        result, _ = upsert_reference(
            _base_record(doi=""),
            db_path=db,
        )
        assert result == UpsertResult.INSERTED


# ---------------------------------------------------------------------------
# discovered_via enum validation
# ---------------------------------------------------------------------------

class TestDiscoveredViaEnum:
    VALID_VALUES = [
        "review_pdf_extract",
        "serpapi_scholar",
        "scholarly_search",
        "paperscraper_search",
        "openalex_expansion",
        "crossref_search",
        "student_upload",
    ]

    @pytest.mark.parametrize("via", VALID_VALUES)
    def test_all_valid_enum_values_insert(self, via, tmp_path):
        db = _db(tmp_path)
        _init(db)
        from lifecycle_db import upsert_reference, UpsertResult
        # Use different DOIs so none collide
        doi = f"10.1/test-{via}"
        result, _ = upsert_reference(
            _base_record(doi=doi, discovered_via=via),
            db_path=db,
        )
        assert result == UpsertResult.INSERTED

    def test_invalid_discovered_via_raises_value_error(self, tmp_path):
        db = _db(tmp_path)
        _init(db)
        from lifecycle_db import upsert_reference
        with pytest.raises(ValueError, match="discovered_via"):
            upsert_reference(
                _base_record(discovered_via="not_a_valid_source"),
                db_path=db,
            )

    def test_empty_discovered_via_raises_value_error(self, tmp_path):
        db = _db(tmp_path)
        _init(db)
        from lifecycle_db import upsert_reference
        with pytest.raises(ValueError, match="discovered_via"):
            upsert_reference(
                _base_record(discovered_via=""),
                db_path=db,
            )


# ---------------------------------------------------------------------------
# write_candidates_to_lifecycle (batch bridge)
# ---------------------------------------------------------------------------

class TestWriteCandidatesToLifecycle:
    def _make_harvest_cand(self, *, doi: str, title: str = "Test Paper",
                           scraper_source: str = "serpapi") -> dict:
        return {
            "paper_id": f"serpapi-abc{doi[-3:]}",
            "gap_id": "GAP-PNU-001",
            "scraper_source": scraper_source,
            "title": title,
            "doi": doi,
            "url": "https://example.com",
            "authors": ["Smith, A."],
            "year": 2023,
            "venue": "Test Journal",
            "snippet": "A test snippet about daylight.",
        }

    def test_batch_inserts_return_inserted_count(self, tmp_path):
        db = _db(tmp_path)
        from lifecycle_db import write_candidates_to_lifecycle
        candidates = [
            self._make_harvest_cand(doi=f"10.1/test-{i}") for i in range(3)
        ]
        counts = write_candidates_to_lifecycle(candidates, db_path=db)
        assert counts["inserted"] == 3
        assert counts["skipped"] == 0
        assert counts["error"] == 0

    def test_unknown_scraper_source_is_skipped(self, tmp_path):
        db = _db(tmp_path)
        from lifecycle_db import write_candidates_to_lifecycle
        candidates = [
            self._make_harvest_cand(doi="10.1/bad", scraper_source="unknown_scraper")
        ]
        counts = write_candidates_to_lifecycle(candidates, db_path=db)
        assert counts["skipped"] == 1
        assert counts["inserted"] == 0

    def test_doi_merge_counted_separately(self, tmp_path):
        db = _db(tmp_path)
        from lifecycle_db import write_candidates_to_lifecycle
        cand = self._make_harvest_cand(doi="10.1/dup")
        # Insert twice with different scraper (second becomes doi_merge)
        write_candidates_to_lifecycle([cand], db_path=db)
        cand2 = dict(cand)
        cand2["scraper_source"] = "scholarly"
        counts = write_candidates_to_lifecycle([cand2], db_path=db)
        assert counts["doi_merge"] == 1

    def test_all_scraper_sources_mapped_correctly(self, tmp_path):
        db = _db(tmp_path)
        from lifecycle_db import write_candidates_to_lifecycle, SCRAPER_SOURCE_MAP
        # Each mapped scraper_source should insert successfully
        candidates = []
        for i, src in enumerate(SCRAPER_SOURCE_MAP.keys()):
            candidates.append(
                self._make_harvest_cand(doi=f"10.1/src-{i}", scraper_source=src)
            )
        counts = write_candidates_to_lifecycle(candidates, db_path=db)
        assert counts["skipped"] == 0
        assert counts["inserted"] == len(SCRAPER_SOURCE_MAP)

    def test_dry_run_does_not_write_to_db(self, tmp_path):
        db = _db(tmp_path)
        from lifecycle_db import write_candidates_to_lifecycle, get_lifecycle_counts
        candidates = [self._make_harvest_cand(doi="10.1/dryrun")]
        write_candidates_to_lifecycle(candidates, db_path=db, dry_run=True)
        # DB should still not exist or be empty
        counts = get_lifecycle_counts(db)
        assert counts["total"] == 0


# ---------------------------------------------------------------------------
# get_lifecycle_counts
# ---------------------------------------------------------------------------

class TestGetLifecycleCounts:
    def test_empty_db_returns_zeros(self, tmp_path):
        db = _db(tmp_path)
        from lifecycle_db import get_lifecycle_counts
        counts = get_lifecycle_counts(db)
        assert counts["total"] == 0
        assert counts["metadata_only"] == 0
        assert counts["duplicate"] == 0

    def test_counts_after_inserts(self, tmp_path):
        db = _db(tmp_path)
        _init(db)
        from lifecycle_db import upsert_reference, get_lifecycle_counts
        for i in range(4):
            upsert_reference(_base_record(doi=f"10.1/paper-{i}"), db_path=db)
        counts = get_lifecycle_counts(db)
        assert counts["total"] == 4
        assert counts["metadata_only"] == 4
        assert counts["duplicate"] == 0

    def test_with_doi_count(self, tmp_path):
        db = _db(tmp_path)
        _init(db)
        from lifecycle_db import upsert_reference, get_lifecycle_counts
        upsert_reference(_base_record(doi="10.1/has-doi"), db_path=db)
        upsert_reference(_base_record(doi=""), db_path=db)
        counts = get_lifecycle_counts(db)
        assert counts["with_doi"] == 1
        assert counts["total"] == 2


# ---------------------------------------------------------------------------
# dry_run mode on upsert_reference
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_dry_run_on_empty_db_returns_inserted(self, tmp_path):
        db = _db(tmp_path)
        from lifecycle_db import upsert_reference, UpsertResult
        result, ref_id = upsert_reference(_base_record(), db_path=db, dry_run=True)
        assert result == UpsertResult.INSERTED
        assert "dry-run" in ref_id.lower()

    def test_dry_run_does_not_write_row(self, tmp_path):
        db = _db(tmp_path)
        _init(db)
        from lifecycle_db import upsert_reference, get_connection
        upsert_reference(_base_record(), db_path=db, dry_run=True)
        with get_connection(db) as conn:
            count = conn.execute("SELECT COUNT(*) FROM article_references").fetchone()[0]
        assert count == 0

    def test_dry_run_predicts_doi_merge(self, tmp_path):
        db = _db(tmp_path)
        _init(db)
        from lifecycle_db import upsert_reference, UpsertResult
        # Real insert first
        upsert_reference(_base_record(), db_path=db)
        # dry_run on same DOI → should predict DOI_MERGE
        result, _ = upsert_reference(
            _base_record(discovered_via="crossref_search"),
            db_path=db,
            dry_run=True,
        )
        assert result == UpsertResult.DOI_MERGE


# ---------------------------------------------------------------------------
# Standalone run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import subprocess, sys
    subprocess.run([sys.executable, "-m", "pytest", __file__, "-v"], check=True)
