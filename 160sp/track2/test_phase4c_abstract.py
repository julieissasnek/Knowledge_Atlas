"""
test_phase4c_abstract.py -- Contract tests for Phase 4C abstract collection.

Coverage map (every contract clause must have at least one test):
  P4C-1  paper_fetcher clients importable and expose correct interface
  P4C-2  SemanticScholarClient rate-limiter is bounded to 20 req/min
  P4C-3  Abstract > 150 chars; truncation-marker rejection; snippet-echo rejection
  P4C-4  Title similarity >= 0.90 enforced (via paper_fetcher._title_ok)
  P4C-5  update_triage_stage() called with correct args; DB written correctly
  P4C-6  MISSING_ABSTRACT on all-source exhaustion; no silent drops
  P4C-7  Hit-rate warning emitted when below 0.70 floor

Additional:
  - Fallback chain short-circuits on first valid hit
  - DOI-path skipped gracefully when doi is empty
  - dry_run mode does not write to DB
  - Injected clients let tests run offline (no real HTTP)
  - abstract_missing rows are NOT re-processed
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

# ── Path fixtures ──────────────────────────────────────────────────────────────

TRACK2 = Path(__file__).resolve().parent
AE_SRC  = TRACK2.parent.parent.parent.parent / "Article_Eater" / "src"

# Inject BEFORE any other local imports so pytest collection can resolve
# `services.paper_fetcher` at module-load time (not just at runtime).
if str(AE_SRC) not in sys.path:
    sys.path.insert(0, str(AE_SRC))

# ── Module imports ─────────────────────────────────────────────────────────────

from abstract_collector_4c import (
    ABSTRACT_MIN_CHARS,
    HIT_RATE_FLOOR,
    SNIPPET_SIM_CEILING,
    _fetch_abstract,
    _is_valid_abstract,
    run_phase4c_abstract_collection,
)
from lifecycle_db import (
    TRIAGE_STAGE_ABSTRACT_COLLECTED,
    TRIAGE_STAGE_ABSTRACT_MISSING,
    TRIAGE_STAGE_ABSTRACT_PENDING,
    get_pending_abstract_collection,
    init_lifecycle_db,
    update_triage_stage,
    upsert_reference,
)
from services.paper_fetcher import (  # type: ignore
    CrossRefClient,
    OpenAlexHelper,
    PubMedClient,
    SemanticScholarClient,
    _RateLimiter,
    _title_ok,
    _title_sim,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

GOOD_ABSTRACT = (
    "This study examines the effects of daylighting on cognitive performance "
    "in open-plan offices. Sixty participants were assessed under three lighting "
    "conditions: daylight, warm LED, and cool LED. Results demonstrate a "
    "statistically significant improvement in sustained attention under daylight "
    "exposure, consistent with circadian entrainment theory."
)  # 352 chars — well above ABSTRACT_MIN_CHARS

SHORT_ABSTRACT  = "Short abstract under limit."          # definitely <= 150 chars
TRUNCATED_GOOD  = GOOD_ABSTRACT[:200] + "..."           # long but truncated
SNIPPET_ECHO    = GOOD_ABSTRACT                         # identical to snippet → echo

VALID_DOI   = "10.1016/j.buildenv.2020.106960"
VALID_TITLE = "Daylight and cognitive performance in offices"


def _make_client(abstract: str = "", source: str = "semantic_scholar") -> MagicMock:
    """Return a mock client whose by_doi and by_title both return the given abstract."""
    result = {"doi": VALID_DOI, "title": VALID_TITLE, "abstract": abstract,
              "year": 2020, "source": source}
    m = MagicMock()
    m.by_doi.return_value  = result if abstract else None
    m.by_title.return_value = result if abstract else None
    return m


def _empty_client(source: str = "semantic_scholar") -> MagicMock:
    """Return a mock client that always returns None."""
    m = MagicMock()
    m.by_doi.return_value   = None
    m.by_title.return_value = None
    return m


def _make_row(
    tmp_db: str,
    *,
    doi: str = VALID_DOI,
    title: str = VALID_TITLE,
    snippet: str = "",
) -> str:
    """Insert a record at abstract_pending stage and return its reference_id."""
    record = {
        "title_raw":        title,
        "doi":              doi,
        "discovered_via":   "serpapi_scholar",
        "snippet":          snippet,
    }
    result, ref_id = upsert_reference(record, db_path=tmp_db)
    # Advance to abstract_pending
    update_triage_stage(ref_id, TRIAGE_STAGE_ABSTRACT_PENDING, db_path=tmp_db)
    return ref_id


# ===========================================================================
# P4C-1: Client interface contract
# ===========================================================================

class TestClientInterface:
    """P4C-1: all four clients expose the required interface."""

    def test_ss_has_by_doi_and_by_title(self):
        ss = SemanticScholarClient()
        assert callable(ss.by_doi)
        assert callable(ss.by_title)

    def test_cr_has_by_doi_and_by_title(self):
        cr = CrossRefClient()
        assert callable(cr.by_doi)
        assert callable(cr.by_title)

    def test_pm_has_by_doi_and_by_title(self):
        pm = PubMedClient()
        assert callable(pm.by_doi)
        assert callable(pm.by_title)

    def test_oa_has_by_doi_and_by_title(self):
        oa = OpenAlexHelper()
        assert callable(oa.by_doi)
        assert callable(oa.by_title)

    def test_none_inputs_return_none(self):
        """All clients must return None gracefully for empty DOI/title."""
        ss = SemanticScholarClient()
        cr = CrossRefClient()
        pm = PubMedClient()
        oa = OpenAlexHelper()
        for client in (ss, cr, pm, oa):
            assert client.by_doi("") is None
            assert client.by_title("") is None


# ===========================================================================
# P4C-2: SemanticScholar rate-limiter bounded to 20 req/min
# ===========================================================================

class TestRateLimiter:
    """P4C-2: _RateLimiter enforces max_calls / period."""

    def test_rate_limiter_allows_up_to_max(self):
        rl = _RateLimiter(max_calls=5, period=60)
        t0 = time.monotonic()
        for _ in range(5):
            rl.acquire()
        elapsed = time.monotonic() - t0
        # Five tokens consumed immediately — should be very fast
        assert elapsed < 1.0

    def test_rate_limiter_blocks_on_overflow(self):
        """The 6th acquisition on a 5/1s limiter must block."""
        rl = _RateLimiter(max_calls=5, period=1)
        for _ in range(5):
            rl.acquire()
        t0 = time.monotonic()
        # This should block until ~1 second has passed
        rl.acquire()
        elapsed = time.monotonic() - t0
        assert elapsed >= 0.5, f"Expected >= 0.5s block, got {elapsed:.3f}s"

    def test_ss_limiter_is_20_per_60s(self):
        """SemanticScholarClient must configure a 20/60s limiter."""
        ss = SemanticScholarClient()
        # Access the private limiter attribute
        limiter = ss._limiter
        assert limiter._max_calls == 20
        assert limiter._period == 60


# ===========================================================================
# P4C-3: Abstract validation
# ===========================================================================

class TestIsValidAbstract:
    """P4C-3: _is_valid_abstract enforces length, truncation, and echo rules."""

    def test_good_abstract_passes(self):
        assert _is_valid_abstract(GOOD_ABSTRACT) is True

    def test_empty_string_fails(self):
        assert _is_valid_abstract("") is False

    def test_none_equivalent_empty_fails(self):
        assert _is_valid_abstract(None) is False  # type: ignore[arg-type]

    def test_below_min_chars_fails(self):
        assert _is_valid_abstract(SHORT_ABSTRACT) is False

    def test_exactly_150_chars_fails(self):
        # > 150, so exactly 150 must fail
        border = "x" * ABSTRACT_MIN_CHARS
        assert _is_valid_abstract(border) is False

    def test_151_chars_passes(self):
        assert _is_valid_abstract("x" * (ABSTRACT_MIN_CHARS + 1)) is True

    @pytest.mark.parametrize("marker", ["...", "[...]", "…", "... [truncated]"])
    def test_trailing_truncation_marker_fails(self, marker):
        text = "x" * (ABSTRACT_MIN_CHARS + 50) + marker
        assert _is_valid_abstract(text) is False

    def test_truncation_marker_mid_text_passes(self):
        # Truncation marker in the middle is allowed
        text = "x" * (ABSTRACT_MIN_CHARS + 10) + "... and more text here."
        assert _is_valid_abstract(text) is True

    def test_snippet_echo_fails(self):
        """Abstract identical to snippet must be rejected."""
        snippet = GOOD_ABSTRACT
        assert _is_valid_abstract(GOOD_ABSTRACT, snippet=snippet) is False

    def test_snippet_high_similarity_fails(self):
        """Abstract very similar (>= 0.85) to snippet must be rejected."""
        # Modify only the last 5% of the string
        cut = int(len(GOOD_ABSTRACT) * 0.97)
        near_echo = GOOD_ABSTRACT[:cut] + " minimal change here end."
        assert _is_valid_abstract(near_echo, snippet=GOOD_ABSTRACT) is False

    def test_unrelated_snippet_passes(self):
        """Distinct snippet should not block a valid abstract."""
        snippet = "Machine learning models for protein folding."
        assert _is_valid_abstract(GOOD_ABSTRACT, snippet=snippet) is True

    def test_no_snippet_skips_echo_check(self):
        """With no snippet argument, echo check is skipped."""
        assert _is_valid_abstract(GOOD_ABSTRACT) is True


# ===========================================================================
# P4C-4: Title similarity guard
# ===========================================================================

class TestTitleSimilarity:
    """P4C-4: _title_ok enforces SequenceMatcher >= 0.90."""

    @pytest.mark.parametrize("a, b, expected", [
        ("Daylight and cognitive performance in offices",
         "Daylight and cognitive performance in offices", True),
        ("Daylight and cognitive performance in offices",
         "Daylight and cognitive performance in office buildings", True),
        ("Daylight and cognitive performance",
         "Deep learning for protein structure prediction", False),
        ("", "Anything", False),
        ("Anything", "", False),
    ])
    def test_title_ok_threshold(self, a, b, expected):
        assert _title_ok(a, b) is expected

    def test_title_sim_symmetric(self):
        a = "Effects of daylight on attention"
        b = "Effects of daylighting on attention span"
        assert abs(_title_sim(a, b) - _title_sim(b, a)) < 1e-9

    def test_identical_titles_score_one(self):
        t = "Some consistent title"
        assert _title_sim(t, t) == pytest.approx(1.0)

    def test_completely_different_scores_low(self):
        assert _title_sim("aardvark biology", "quantum entanglement") < 0.5


# ===========================================================================
# P4C-5: DB write contract
# ===========================================================================

class TestDbWriteContract:
    """P4C-5: update_triage_stage writes abstract and source to DB."""

    def test_collected_written_to_db(self, tmp_path):
        db = str(tmp_path / "lc.db")
        init_lifecycle_db(db)
        ref_id = _make_row(db)

        update_triage_stage(
            ref_id,
            TRIAGE_STAGE_ABSTRACT_COLLECTED,
            abstract=GOOD_ABSTRACT,
            abstract_source="semantic_scholar",
            db_path=db,
        )

        from lifecycle_db import get_connection
        with get_connection(db) as conn:
            row = conn.execute(
                "SELECT triage_stage, abstract, abstract_source, phase4b_at "
                "FROM article_references WHERE reference_id = ?",
                (ref_id,),
            ).fetchone()

        assert row["triage_stage"]    == TRIAGE_STAGE_ABSTRACT_COLLECTED
        assert row["abstract"]        == GOOD_ABSTRACT
        assert row["abstract_source"] == "semantic_scholar"
        assert row["phase4b_at"] is not None

    def test_missing_written_to_db(self, tmp_path):
        db = str(tmp_path / "lc.db")
        init_lifecycle_db(db)
        ref_id = _make_row(db)

        update_triage_stage(
            ref_id,
            TRIAGE_STAGE_ABSTRACT_MISSING,
            db_path=db,
        )

        from lifecycle_db import get_connection
        with get_connection(db) as conn:
            row = conn.execute(
                "SELECT triage_stage FROM article_references WHERE reference_id = ?",
                (ref_id,),
            ).fetchone()

        assert row["triage_stage"] == TRIAGE_STAGE_ABSTRACT_MISSING


# ===========================================================================
# P4C-6: Missing abstract — no silent drops
# ===========================================================================

class TestNoSilentDrops:
    """P4C-6: every exhausted row must land in abstract_missing, not disappear."""

    def test_all_sources_return_none_yields_missing(self):
        """_fetch_abstract with all-None clients returns ('', '')."""
        result = _fetch_abstract(
            VALID_DOI, VALID_TITLE,
            ss=_empty_client("semantic_scholar"),
            cr=_empty_client("crossref"),
            pm=_empty_client("pubmed"),
            oa=_empty_client("openalex"),
            delay=0,
        )
        assert result == ("", "")

    def test_run_writes_missing_for_unresolvable(self, tmp_path):
        db = str(tmp_path / "lc.db")
        init_lifecycle_db(db)
        ref_id = _make_row(db)

        counts = run_phase4c_abstract_collection(
            db,
            delay=0,
            ss=_empty_client(),
            cr=_empty_client(),
            pm=_empty_client(),
            oa=_empty_client(),
        )

        assert counts[TRIAGE_STAGE_ABSTRACT_MISSING] == 1
        assert counts[TRIAGE_STAGE_ABSTRACT_COLLECTED] == 0

        from lifecycle_db import get_connection
        with get_connection(db) as conn:
            stage = conn.execute(
                "SELECT triage_stage FROM article_references WHERE reference_id = ?",
                (ref_id,),
            ).fetchone()["triage_stage"]
        assert stage == TRIAGE_STAGE_ABSTRACT_MISSING

    def test_row_with_no_title_and_no_doi_marked_missing(self, tmp_path):
        """Rows with neither DOI nor title go straight to abstract_missing."""
        db = str(tmp_path / "lc.db")
        init_lifecycle_db(db)

        record = {"title_raw": "", "doi": "", "discovered_via": "serpapi_scholar"}
        _, ref_id = upsert_reference(record, db_path=db)
        update_triage_stage(ref_id, TRIAGE_STAGE_ABSTRACT_PENDING, db_path=db)

        counts = run_phase4c_abstract_collection(
            db, delay=0,
            ss=_empty_client(), cr=_empty_client(),
            pm=_empty_client(), oa=_empty_client(),
        )
        assert counts[TRIAGE_STAGE_ABSTRACT_MISSING] >= 1


# ===========================================================================
# Fallback chain short-circuit behaviour
# ===========================================================================

class TestFallbackChain:
    """Ensure chain short-circuits on first valid hit and does not over-call."""

    def test_ss_doi_hit_skips_remaining_sources(self):
        ss = _make_client(GOOD_ABSTRACT, "semantic_scholar")
        cr = _empty_client("crossref")
        pm = _empty_client("pubmed")
        oa = _empty_client("openalex")

        abstract, source = _fetch_abstract(
            VALID_DOI, VALID_TITLE,
            ss=ss, cr=cr, pm=pm, oa=oa, delay=0,
        )

        assert abstract == GOOD_ABSTRACT
        assert source   == "semantic_scholar"
        ss.by_doi.assert_called_once()
        cr.by_doi.assert_not_called()
        pm.by_doi.assert_not_called()
        oa.by_doi.assert_not_called()

    def test_falls_through_to_crossref_when_ss_empty(self):
        ss = _empty_client("semantic_scholar")
        cr = _make_client(GOOD_ABSTRACT, "crossref")
        pm = _empty_client("pubmed")
        oa = _empty_client("openalex")

        abstract, source = _fetch_abstract(
            VALID_DOI, VALID_TITLE,
            ss=ss, cr=cr, pm=pm, oa=oa, delay=0,
        )

        assert abstract == GOOD_ABSTRACT
        assert source   == "crossref"
        pm.by_doi.assert_not_called()
        oa.by_doi.assert_not_called()

    def test_falls_through_to_pubmed(self):
        ss = _empty_client("semantic_scholar")
        cr = _empty_client("crossref")
        pm = _make_client(GOOD_ABSTRACT, "pubmed")
        oa = _empty_client("openalex")

        abstract, source = _fetch_abstract(
            VALID_DOI, VALID_TITLE,
            ss=ss, cr=cr, pm=pm, oa=oa, delay=0,
        )

        assert source == "pubmed"
        oa.by_doi.assert_not_called()

    def test_falls_through_to_openalex(self):
        ss = _empty_client("semantic_scholar")
        cr = _empty_client("crossref")
        pm = _empty_client("pubmed")
        oa = _make_client(GOOD_ABSTRACT, "openalex")

        abstract, source = _fetch_abstract(
            VALID_DOI, VALID_TITLE,
            ss=ss, cr=cr, pm=pm, oa=oa, delay=0,
        )

        assert source == "openalex"

    def test_truncated_abstract_not_accepted_falls_through(self):
        """A result that fails _is_valid_abstract must not short-circuit."""
        ss = _make_client(TRUNCATED_GOOD, "semantic_scholar")
        cr = _make_client(GOOD_ABSTRACT, "crossref")
        pm = _empty_client("pubmed")
        oa = _empty_client("openalex")

        abstract, source = _fetch_abstract(
            VALID_DOI, VALID_TITLE,
            ss=ss, cr=cr, pm=pm, oa=oa, delay=0,
        )

        assert source == "crossref"   # SS result rejected; CrossRef accepted

    def test_doi_absent_skips_doi_calls(self):
        """When doi='', by_doi must never be called on any client."""
        ss = _make_client(GOOD_ABSTRACT, "semantic_scholar")
        cr = _empty_client("crossref")
        pm = _empty_client("pubmed")
        oa = _empty_client("openalex")

        _fetch_abstract(
            "", VALID_TITLE,
            ss=ss, cr=cr, pm=pm, oa=oa, delay=0,
        )

        ss.by_doi.assert_not_called()
        cr.by_doi.assert_not_called()
        pm.by_doi.assert_not_called()
        oa.by_doi.assert_not_called()


# ===========================================================================
# Batch orchestrator: correct rows only
# ===========================================================================

class TestBatchOrchestrator:
    """run_phase4c_abstract_collection processes only abstract_pending rows."""

    def test_processes_only_abstract_pending(self, tmp_path):
        """abstract_missing and abstract_collected rows must not be touched."""
        db = str(tmp_path / "lc.db")
        init_lifecycle_db(db)

        # Insert one pending and one already-missing
        pending_id = _make_row(db, title="Pending paper on daylight")
        missing_id = _make_row(db, doi="10.1000/missing", title="Already resolved paper")
        update_triage_stage(missing_id, TRIAGE_STAGE_ABSTRACT_MISSING, db_path=db)

        counts = run_phase4c_abstract_collection(
            db, delay=0,
            ss=_make_client(GOOD_ABSTRACT),
            cr=_empty_client(), pm=_empty_client(), oa=_empty_client(),
        )

        # Only the pending row should be processed
        assert counts[TRIAGE_STAGE_ABSTRACT_COLLECTED] == 1
        assert counts[TRIAGE_STAGE_ABSTRACT_MISSING]   == 0  # missing row untouched

    def test_multiple_rows_all_resolved(self, tmp_path):
        db = str(tmp_path / "lc.db")
        init_lifecycle_db(db)
        for i in range(4):
            _make_row(db, doi=f"10.1000/test{i}", title=f"Paper about daylight {i}")

        counts = run_phase4c_abstract_collection(
            db, delay=0,
            ss=_make_client(GOOD_ABSTRACT),
            cr=_empty_client(), pm=_empty_client(), oa=_empty_client(),
        )

        assert counts[TRIAGE_STAGE_ABSTRACT_COLLECTED] == 4
        assert counts[TRIAGE_STAGE_ABSTRACT_MISSING]   == 0

    def test_multiple_rows_none_resolved(self, tmp_path):
        db = str(tmp_path / "lc.db")
        init_lifecycle_db(db)
        for i in range(3):
            _make_row(db, doi=f"10.1000/miss{i}", title=f"Unresolvable paper {i}")

        counts = run_phase4c_abstract_collection(
            db, delay=0,
            ss=_empty_client(), cr=_empty_client(),
            pm=_empty_client(), oa=_empty_client(),
        )

        assert counts[TRIAGE_STAGE_ABSTRACT_MISSING]   == 3
        assert counts[TRIAGE_STAGE_ABSTRACT_COLLECTED] == 0

    def test_dry_run_does_not_write(self, tmp_path):
        db = str(tmp_path / "lc.db")
        init_lifecycle_db(db)
        ref_id = _make_row(db)

        run_phase4c_abstract_collection(
            db, delay=0, dry_run=True,
            ss=_make_client(GOOD_ABSTRACT),
            cr=_empty_client(), pm=_empty_client(), oa=_empty_client(),
        )

        # Row must still be abstract_pending after dry_run
        from lifecycle_db import get_connection
        with get_connection(db) as conn:
            stage = conn.execute(
                "SELECT triage_stage FROM article_references WHERE reference_id = ?",
                (ref_id,),
            ).fetchone()["triage_stage"]
        assert stage == TRIAGE_STAGE_ABSTRACT_PENDING


# ===========================================================================
# P4C-7: Hit-rate floor reporting
# ===========================================================================

class TestHitRateReporting:
    """P4C-7: warn when DOI-record hit rate < 0.70."""

    def test_hit_rate_warning_emitted(self, tmp_path, capsys):
        """When hit rate < 0.70 on DOI records, warning line must appear."""
        db = str(tmp_path / "lc.db")
        init_lifecycle_db(db)

        # 3 DOI records; 0 will get an abstract
        for i in range(3):
            _make_row(db, doi=f"10.1000/r{i}", title=f"Paper {i}")

        run_phase4c_abstract_collection(
            db, delay=0,
            ss=_empty_client(), cr=_empty_client(),
            pm=_empty_client(), oa=_empty_client(),
        )

        captured = capsys.readouterr()
        assert "WARNING" in captured.out or "WARNING" in captured.err

    def test_no_warning_above_floor(self, tmp_path, capsys):
        """When hit rate >= 0.70, no WARNING line should appear."""
        db = str(tmp_path / "lc.db")
        init_lifecycle_db(db)

        # 3 DOI records; all 3 get an abstract → 100% hit rate
        for i in range(3):
            _make_row(db, doi=f"10.1000/r{i}", title=f"Paper {i}")

        run_phase4c_abstract_collection(
            db, delay=0,
            ss=_make_client(GOOD_ABSTRACT),
            cr=_empty_client(), pm=_empty_client(), oa=_empty_client(),
        )

        captured = capsys.readouterr()
        assert "WARNING" not in captured.out
        assert "WARNING" not in captured.err

    def test_hit_rate_floor_constant(self):
        assert HIT_RATE_FLOOR == pytest.approx(0.70)


# ===========================================================================
# Audit: defects fixed from abstract_collector.py (legacy)
# ===========================================================================

class TestLegacyDefectsFixed:
    """
    Verify that the defects found in the old abstract_collector.py are
    not reproducible in abstract_collector_4c.py.
    """

    def test_no_snippet_echo_on_exhaustion(self, tmp_path):
        """
        OLD BUG: on all-source failure, abstract_collector.py returned the
        SerpAPI snippet as the abstract with enriched=False.

        NEW BEHAVIOUR: _fetch_abstract returns ('', '') on exhaustion.
        The orchestrator then writes MISSING, not a snippet.
        """
        db = str(tmp_path / "lc.db")
        init_lifecycle_db(db)
        snippet = "Daylight improves focus in office settings."
        ref_id  = _make_row(db, snippet=snippet)

        run_phase4c_abstract_collection(
            db, delay=0,
            ss=_empty_client(), cr=_empty_client(),
            pm=_empty_client(), oa=_empty_client(),
        )

        from lifecycle_db import get_connection
        with get_connection(db) as conn:
            row = conn.execute(
                "SELECT triage_stage, abstract FROM article_references "
                "WHERE reference_id = ?",
                (ref_id,),
            ).fetchone()

        assert row["triage_stage"] == TRIAGE_STAGE_ABSTRACT_MISSING
        assert row["abstract"] is None   # no snippet echoed into abstract column

    def test_short_snippet_not_accepted_as_abstract(self):
        """
        OLD BUG: word_count < 20 was the minimum (snippets of 30-50 words passed).
        NEW BEHAVIOUR: _is_valid_abstract requires > 150 characters.
        """
        # A 50-word snippet is roughly 300 chars — but let's test a typical snippet
        short_snippet = (
            "Daylight exposure in classrooms was linked to improved academic performance. "
            "The study controlled for confounding variables including room temperature and noise."
        )  # 161 chars — clears ABSTRACT_MIN_CHARS (150)
        assert len(short_snippet) > ABSTRACT_MIN_CHARS  # this one actually is long enough
        # Confirm a genuinely short snippet (25 words, ~160 chars) passes length
        # but would be caught by other checks if it were a snippet echo
        ok = _is_valid_abstract(short_snippet)
        assert ok is True  # 156 chars, no truncation, no echo = valid

        # Now the canonical "25 word" snippet that the old code accepted
        really_short = "Daylight improves focus. Study shows results."   # 45 chars
        assert _is_valid_abstract(really_short) is False

    def test_rate_limiter_present_on_ss_client(self):
        """OLD BUG: no rate-limit on Semantic Scholar. NEW: _RateLimiter enforced."""
        ss = SemanticScholarClient()
        assert hasattr(ss, "_limiter")
        assert isinstance(ss._limiter, _RateLimiter)

    def test_abstract_source_field_consistent(self, tmp_path):
        """
        OLD BUG: failure path used 'source_enrichment', success used 'source'.
        NEW: abstract_source column in lifecycle_db is always set consistently.
        """
        db = str(tmp_path / "lc.db")
        init_lifecycle_db(db)
        ref_id = _make_row(db)

        run_phase4c_abstract_collection(
            db, delay=0,
            ss=_make_client(GOOD_ABSTRACT, "semantic_scholar"),
            cr=_empty_client(), pm=_empty_client(), oa=_empty_client(),
        )

        from lifecycle_db import get_connection
        with get_connection(db) as conn:
            row = conn.execute(
                "SELECT abstract_source FROM article_references WHERE reference_id = ?",
                (ref_id,),
            ).fetchone()
        assert row["abstract_source"] == "semantic_scholar"

    def test_phase4b_at_timestamp_written(self, tmp_path):
        """OLD BUG: no abstract_collected_at audit timestamp. NEW: phase4b_at written."""
        db = str(tmp_path / "lc.db")
        init_lifecycle_db(db)
        ref_id = _make_row(db)

        run_phase4c_abstract_collection(
            db, delay=0,
            ss=_make_client(GOOD_ABSTRACT),
            cr=_empty_client(), pm=_empty_client(), oa=_empty_client(),
        )

        from lifecycle_db import get_connection
        with get_connection(db) as conn:
            row = conn.execute(
                "SELECT phase4b_at FROM article_references WHERE reference_id = ?",
                (ref_id,),
            ).fetchone()
        assert row["phase4b_at"] is not None
