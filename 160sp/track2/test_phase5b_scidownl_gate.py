"""
test_phase5b_scidownl_gate.py -- Phase 5B scidownl policy gate contract tests.

Coverage matrix:
  C1-pass   YAML exists, enable_paid_or_grey_sources: true -> passes
  C1-fail-missing   YAML file absent -> ConfigNotArmedError
  C1-fail-false     enable_paid_or_grey_sources: false -> ConfigNotArmedError
  C1-fail-absent    key missing from YAML -> ConfigNotArmedError
  C1-fail-string    value is "true" (string) not true (bool) -> ConfigNotArmedError
  C2-pass   policy_clearance.json exists -> passes
  C2-fail   policy_clearance.json absent -> PolicyClearanceMissingError
  C3-pass   both unpaywall+openalex_oa logged as failure -> passes
  C3-fail-none    no entries at all -> CascadeNotExhaustedError
  C3-fail-partial only unpaywall logged -> CascadeNotExhaustedError
  C3-fail-success unpaywall logged as success (not failure) -> still blocks
  C4-pass   phase4d_decision='ACCEPT' -> passes
  C4-fail-edge     EDGE_CASE -> TriageElevationError
  C4-fail-reject   REJECT -> TriageElevationError
  C4-fail-missing  MISSING_ABSTRACT -> TriageElevationError
  C4-fail-empty    '' -> TriageElevationError
  ALL-pass  all four conditions satisfied -> returns True
  ORDER     conditions checked C1->C2->C3->C4 (first failure wins)
  EXCEPTION-types  each condition raises its specific subclass
  ENGINE-integration  acquire_pdf_for_record uses gate; logs gate_blocked transition
  ENGINE-no-increment  gate_blocked does NOT increment pdf_acquisition_attempts
  ENGINE-scidownl-called  gate passes -> scidownl_fn called
  ENGINE-transition-log  success/failure logged to lifecycle_transitions
  ALIAS     Phase5BPolicyGateError is ScidownlClearanceError (backward compat)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ── Path setup ─────────────────────────────────────────────────────────────────

TRACK2 = Path(__file__).resolve().parent
AE_SRC = TRACK2.parent.parent.parent.parent / "Article_Eater" / "src"
if str(AE_SRC) not in sys.path:
    sys.path.insert(0, str(AE_SRC))

# ── Imports ────────────────────────────────────────────────────────────────────

from scidownl_policy_gate import (
    CascadeNotExhaustedError,
    ConfigNotArmedError,
    PolicyClearanceMissingError,
    ScidownlClearanceError,
    TriageElevationError,
    verify_scidownl_clearance,
)
from lifecycle_db import (
    get_acquisition_attempts_for_record,
    get_connection,
    init_lifecycle_db,
    log_transition,
    upsert_reference,
)
from pdf_acquisition_engine import (
    Phase5BPolicyGateError,
    acquire_pdf_for_record,
)

# ── Fixtures ───────────────────────────────────────────────────────────────────

VALID_PDF = b"%PDF-1.4\n" + b"x" * 2000 + b"\n%%EOF"
VALID_DOI = "10.1016/j.buildenv.2022.109000"


def _write_config(path: str, armed: bool = True) -> None:
    flag = "true" if armed else "false"
    Path(path).write_text(
        f"enable_paid_or_grey_sources: {flag}\nscidownl_daily_limit: 50\n",
        encoding="utf-8",
    )


def _write_clearance(path: str) -> None:
    Path(path).write_text(
        json.dumps({"clearance_issued_by": "test", "clearance_date": "2026-01-01"}),
        encoding="utf-8",
    )


def _seed_accept_row(db: str, doi: str = VALID_DOI) -> str:
    init_lifecycle_db(db)
    _, ref_id = upsert_reference(
        {"title_raw": "Test paper", "doi": doi, "discovered_via": "serpapi_scholar"},
        db_path=db,
    )
    with get_connection(db) as conn:
        conn.execute(
            "UPDATE article_references SET phase4d_decision='ACCEPT', "
            "triage_stage='accepted_for_download' WHERE reference_id=?",
            (ref_id,),
        )
    return ref_id


def _log_both_failures(db: str, ref_id: str, doi: str = VALID_DOI) -> None:
    log_transition(ref_id, "unpaywall",   "failure", doi=doi, db_path=db)
    log_transition(ref_id, "openalex_oa", "failure", doi=doi, db_path=db)


def _gate_kwargs(tmp_path, db: str, ref_id: str) -> dict:
    """Return a fully-armed kwargs dict for verify_scidownl_clearance."""
    cfg = str(tmp_path / "acquisition_config.yaml")
    clr = str(tmp_path / "policy_clearance.json")
    _write_config(cfg, armed=True)
    _write_clearance(clr)
    _log_both_failures(db, ref_id)
    return {
        "db_path": db,
        "config_path": cfg,
        "policy_clearance_path": clr,
    }


# ===========================================================================
# C1: YAML config
# ===========================================================================

class TestCondition1Config:
    """C1: YAML config must exist with enable_paid_or_grey_sources: true."""

    def test_c1_passes_when_armed(self, tmp_path):
        db  = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        ref = _seed_accept_row(db)
        kw  = _gate_kwargs(tmp_path, db, ref)
        # Should not raise
        verify_scidownl_clearance(ref, "ACCEPT", **kw)

    def test_c1_raises_when_yaml_missing(self, tmp_path):
        db  = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        ref = _seed_accept_row(db)
        kw  = _gate_kwargs(tmp_path, db, ref)
        kw["config_path"] = str(tmp_path / "nonexistent.yaml")
        with pytest.raises(ConfigNotArmedError) as exc_info:
            verify_scidownl_clearance(ref, "ACCEPT", **kw)
        assert ref in str(exc_info.value)

    def test_c1_raises_when_false(self, tmp_path):
        db  = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        ref = _seed_accept_row(db)
        kw  = _gate_kwargs(tmp_path, db, ref)
        _write_config(kw["config_path"], armed=False)  # overwrite with false
        with pytest.raises(ConfigNotArmedError):
            verify_scidownl_clearance(ref, "ACCEPT", **kw)

    def test_c1_raises_when_key_absent(self, tmp_path):
        db  = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        ref = _seed_accept_row(db)
        kw  = _gate_kwargs(tmp_path, db, ref)
        # Write a YAML with completely unrelated keys
        Path(kw["config_path"]).write_text(
            "scidownl_daily_limit: 50\nother_key: value\n", encoding="utf-8"
        )
        with pytest.raises(ConfigNotArmedError) as exc_info:
            verify_scidownl_clearance(ref, "ACCEPT", **kw)
        assert "absent" in str(exc_info.value).lower() or "missing" in str(exc_info.value).lower()

    def test_c1_raises_when_value_is_string_true(self, tmp_path):
        """The string 'true' is not boolean true -- must reject."""
        db  = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        ref = _seed_accept_row(db)
        kw  = _gate_kwargs(tmp_path, db, ref)
        # YAML with quoted string value
        Path(kw["config_path"]).write_text(
            'enable_paid_or_grey_sources: "true"\n', encoding="utf-8"
        )
        with pytest.raises(ConfigNotArmedError):
            verify_scidownl_clearance(ref, "ACCEPT", **kw)

    def test_c1_error_is_subclass_of_clearance_error(self, tmp_path):
        db  = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        ref = _seed_accept_row(db)
        kw  = _gate_kwargs(tmp_path, db, ref)
        kw["config_path"] = str(tmp_path / "no.yaml")
        with pytest.raises(ScidownlClearanceError):
            verify_scidownl_clearance(ref, "ACCEPT", **kw)

    def test_c1_condition_number(self):
        assert ConfigNotArmedError.condition == 1


# ===========================================================================
# C2: policy_clearance.json
# ===========================================================================

class TestCondition2PolicyClearance:
    """C2: policy_clearance.json must exist at the project root path."""

    def test_c2_passes_when_file_present(self, tmp_path):
        db  = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        ref = _seed_accept_row(db)
        kw  = _gate_kwargs(tmp_path, db, ref)
        verify_scidownl_clearance(ref, "ACCEPT", **kw)  # must not raise

    def test_c2_raises_when_file_absent(self, tmp_path):
        db  = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        ref = _seed_accept_row(db)
        kw  = _gate_kwargs(tmp_path, db, ref)
        kw["policy_clearance_path"] = str(tmp_path / "no_clearance.json")
        with pytest.raises(PolicyClearanceMissingError) as exc_info:
            verify_scidownl_clearance(ref, "ACCEPT", **kw)
        assert ref in str(exc_info.value)

    def test_c2_empty_file_still_passes(self, tmp_path):
        """Existence is the unlock; content is not validated by the gate."""
        db  = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        ref = _seed_accept_row(db)
        kw  = _gate_kwargs(tmp_path, db, ref)
        Path(kw["policy_clearance_path"]).write_bytes(b"")  # empty file
        verify_scidownl_clearance(ref, "ACCEPT", **kw)  # must not raise

    def test_c2_error_is_subclass(self):
        assert issubclass(PolicyClearanceMissingError, ScidownlClearanceError)

    def test_c2_condition_number(self):
        assert PolicyClearanceMissingError.condition == 2

    def test_c2_error_message_contains_path(self, tmp_path):
        db  = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        ref = _seed_accept_row(db)
        kw  = _gate_kwargs(tmp_path, db, ref)
        missing_path = str(tmp_path / "missing.json")
        kw["policy_clearance_path"] = missing_path
        with pytest.raises(PolicyClearanceMissingError) as exc_info:
            verify_scidownl_clearance(ref, "ACCEPT", **kw)
        assert missing_path in str(exc_info.value)


# ===========================================================================
# C3: cascade exhaustion
# ===========================================================================

class TestCondition3CascadeExhaustion:
    """C3: Both unpaywall and openalex_oa must be logged as 'failure'."""

    def test_c3_passes_when_both_logged(self, tmp_path):
        db  = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        ref = _seed_accept_row(db)
        kw  = _gate_kwargs(tmp_path, db, ref)
        verify_scidownl_clearance(ref, "ACCEPT", **kw)  # must not raise

    def test_c3_raises_when_no_entries(self, tmp_path):
        db  = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        ref = _seed_accept_row(db)
        cfg = str(tmp_path / "acquisition_config.yaml")
        clr = str(tmp_path / "policy_clearance.json")
        _write_config(cfg, armed=True)
        _write_clearance(clr)
        # Do NOT log any transitions
        with pytest.raises(CascadeNotExhaustedError) as exc_info:
            verify_scidownl_clearance(ref, "ACCEPT",
                                      db_path=db, config_path=cfg,
                                      policy_clearance_path=clr)
        assert "unpaywall" in str(exc_info.value)

    def test_c3_raises_when_only_unpaywall_logged(self, tmp_path):
        db  = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        ref = _seed_accept_row(db)
        cfg = str(tmp_path / "acquisition_config.yaml")
        clr = str(tmp_path / "policy_clearance.json")
        _write_config(cfg, armed=True); _write_clearance(clr)
        log_transition(ref, "unpaywall", "failure", db_path=db)  # only unpaywall
        with pytest.raises(CascadeNotExhaustedError) as exc_info:
            verify_scidownl_clearance(ref, "ACCEPT",
                                      db_path=db, config_path=cfg,
                                      policy_clearance_path=clr)
        assert "openalex_oa" in str(exc_info.value)

    def test_c3_raises_when_only_openalex_logged(self, tmp_path):
        db  = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        ref = _seed_accept_row(db)
        cfg = str(tmp_path / "acquisition_config.yaml")
        clr = str(tmp_path / "policy_clearance.json")
        _write_config(cfg, armed=True); _write_clearance(clr)
        log_transition(ref, "openalex_oa", "failure", db_path=db)  # only openalex
        with pytest.raises(CascadeNotExhaustedError) as exc_info:
            verify_scidownl_clearance(ref, "ACCEPT",
                                      db_path=db, config_path=cfg,
                                      policy_clearance_path=clr)
        assert "unpaywall" in str(exc_info.value)

    def test_c3_raises_when_unpaywall_success_not_failure(self, tmp_path):
        """A 'success' entry for unpaywall does not satisfy the failure requirement."""
        db  = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        ref = _seed_accept_row(db)
        cfg = str(tmp_path / "acquisition_config.yaml")
        clr = str(tmp_path / "policy_clearance.json")
        _write_config(cfg, armed=True); _write_clearance(clr)
        log_transition(ref, "unpaywall",   "success", db_path=db)  # success, not failure
        log_transition(ref, "openalex_oa", "failure", db_path=db)
        with pytest.raises(CascadeNotExhaustedError):
            verify_scidownl_clearance(ref, "ACCEPT",
                                      db_path=db, config_path=cfg,
                                      policy_clearance_path=clr)

    def test_c3_condition_number(self):
        assert CascadeNotExhaustedError.condition == 3

    def test_c3_error_names_missing_source(self, tmp_path):
        db  = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        ref = _seed_accept_row(db)
        cfg = str(tmp_path / "acquisition_config.yaml")
        clr = str(tmp_path / "policy_clearance.json")
        _write_config(cfg, armed=True); _write_clearance(clr)
        with pytest.raises(CascadeNotExhaustedError) as exc_info:
            verify_scidownl_clearance(ref, "ACCEPT",
                                      db_path=db, config_path=cfg,
                                      policy_clearance_path=clr)
        msg = str(exc_info.value)
        assert "unpaywall" in msg and "openalex_oa" in msg


# ===========================================================================
# C4: triage elevation
# ===========================================================================

class TestCondition4TriageElevation:
    """C4: phase4d_decision must be exactly 'ACCEPT'."""

    @pytest.mark.parametrize("decision", [
        "EDGE_CASE", "REJECT", "MISSING_ABSTRACT", "", "accept", "Accept", None,
    ])
    def test_c4_raises_for_non_accept(self, tmp_path, decision):
        db  = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        ref = _seed_accept_row(db)
        kw  = _gate_kwargs(tmp_path, db, ref)
        with pytest.raises(TriageElevationError) as exc_info:
            verify_scidownl_clearance(ref, decision, **kw)
        assert ref in str(exc_info.value)

    def test_c4_passes_for_accept(self, tmp_path):
        db  = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        ref = _seed_accept_row(db)
        kw  = _gate_kwargs(tmp_path, db, ref)
        result = verify_scidownl_clearance(ref, "ACCEPT", **kw)
        assert result is True

    def test_c4_condition_number(self):
        assert TriageElevationError.condition == 4

    def test_c4_names_the_actual_decision(self, tmp_path):
        db  = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        ref = _seed_accept_row(db)
        kw  = _gate_kwargs(tmp_path, db, ref)
        with pytest.raises(TriageElevationError) as exc_info:
            verify_scidownl_clearance(ref, "EDGE_CASE", **kw)
        assert "EDGE_CASE" in str(exc_info.value)


# ===========================================================================
# ALL FOUR PASS: returns True
# ===========================================================================

class TestAllConditionsPass:
    def test_all_four_pass_returns_true(self, tmp_path):
        db  = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        ref = _seed_accept_row(db)
        kw  = _gate_kwargs(tmp_path, db, ref)
        assert verify_scidownl_clearance(ref, "ACCEPT", **kw) is True

    def test_reference_id_in_all_exception_messages(self, tmp_path):
        """Every clearance exception embeds the reference_id."""
        db  = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        ref = _seed_accept_row(db)

        # C1
        with pytest.raises(ConfigNotArmedError) as e:
            verify_scidownl_clearance(ref, "ACCEPT",
                                      db_path=db,
                                      config_path=str(tmp_path / "no.yaml"),
                                      policy_clearance_path=str(tmp_path / "pc.json"))
        assert ref in str(e.value)

        # C2 (armed config, missing clearance)
        cfg = str(tmp_path / "cfg.yaml"); _write_config(cfg)
        with pytest.raises(PolicyClearanceMissingError) as e:
            verify_scidownl_clearance(ref, "ACCEPT",
                                      db_path=db, config_path=cfg,
                                      policy_clearance_path=str(tmp_path / "no.json"))
        assert ref in str(e.value)

        # C3 (both files present, no transitions logged)
        clr = str(tmp_path / "pc.json"); _write_clearance(clr)
        with pytest.raises(CascadeNotExhaustedError) as e:
            verify_scidownl_clearance(ref, "ACCEPT",
                                      db_path=db, config_path=cfg,
                                      policy_clearance_path=clr)
        assert ref in str(e.value)

        # C4 (all present + cascade logged, wrong decision)
        _log_both_failures(db, ref)
        with pytest.raises(TriageElevationError) as e:
            verify_scidownl_clearance(ref, "REJECT",
                                      db_path=db, config_path=cfg,
                                      policy_clearance_path=clr)
        assert ref in str(e.value)


# ===========================================================================
# CONDITION ORDER: C1 checked before C2, C2 before C3, C3 before C4
# ===========================================================================

class TestConditionOrder:
    """First failure wins; later conditions are not evaluated."""

    def test_c1_before_c2(self, tmp_path):
        """Missing config raises C1, not C2, even when clearance is also absent."""
        db  = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        ref = _seed_accept_row(db)
        with pytest.raises(ConfigNotArmedError):
            verify_scidownl_clearance(ref, "ACCEPT",
                                      db_path=db,
                                      config_path=str(tmp_path / "no.yaml"),
                                      policy_clearance_path=str(tmp_path / "no.json"))

    def test_c2_before_c3(self, tmp_path):
        """Missing clearance raises C2, not C3, even when cascade is also missing."""
        db  = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        ref = _seed_accept_row(db)
        cfg = str(tmp_path / "cfg.yaml"); _write_config(cfg)
        with pytest.raises(PolicyClearanceMissingError):
            verify_scidownl_clearance(ref, "ACCEPT",
                                      db_path=db, config_path=cfg,
                                      policy_clearance_path=str(tmp_path / "no.json"))

    def test_c3_before_c4(self, tmp_path):
        """Missing cascade raises C3, not C4, even when decision is also wrong."""
        db  = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        ref = _seed_accept_row(db)
        cfg = str(tmp_path / "cfg.yaml"); _write_config(cfg)
        clr = str(tmp_path / "pc.json"); _write_clearance(clr)
        # No cascade transitions + wrong decision: C3 fires first
        with pytest.raises(CascadeNotExhaustedError):
            verify_scidownl_clearance(ref, "EDGE_CASE",
                                      db_path=db, config_path=cfg,
                                      policy_clearance_path=clr)


# ===========================================================================
# ENGINE INTEGRATION: acquire_pdf_for_record wires the gate
# ===========================================================================

class TestEngineIntegration:
    """The engine must log transitions and respect the gate."""

    def _make_up(self, success: bool) -> MagicMock:
        m = MagicMock()
        if success:
            def _ok(doi, out):
                Path(out).parent.mkdir(parents=True, exist_ok=True)
                Path(out).write_bytes(VALID_PDF)
                return out
            m.try_download.side_effect = _ok
        else:
            m.try_download.return_value = None
        return m

    def test_gate_blocked_does_not_increment_attempt_counter(self, tmp_path):
        db     = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        ref    = _seed_accept_row(db)
        sci_fn = MagicMock(return_value=None)

        # Steps 1+2 fail; gate blocks scidownl (config not armed)
        acquire_pdf_for_record(
            {"reference_id": ref, "doi": VALID_DOI, "title_raw": "T", "phase4d_decision": "ACCEPT"},
            db_path=db,
            pdf_dir=str(tmp_path / "pdfs"),
            unpaywall_client=self._make_up(False),
            openalex_client=self._make_up(False),
            scidownl_fn=sci_fn,
            config_path=str(tmp_path / "no.yaml"),   # C1 will fail
            policy_clearance_path=str(tmp_path / "no.json"),
        )

        sci_fn.assert_not_called()

        with get_connection(db) as conn:
            attempts = conn.execute(
                "SELECT pdf_acquisition_attempts FROM article_references WHERE reference_id=?",
                (ref,),
            ).fetchone()["pdf_acquisition_attempts"]
        assert attempts == 2   # only unpaywall + openalex

    def test_gate_blocked_logs_gate_blocked_transition(self, tmp_path):
        db  = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        ref = _seed_accept_row(db)

        acquire_pdf_for_record(
            {"reference_id": ref, "doi": VALID_DOI, "title_raw": "T", "phase4d_decision": "ACCEPT"},
            db_path=db,
            pdf_dir=str(tmp_path / "pdfs"),
            unpaywall_client=self._make_up(False),
            openalex_client=self._make_up(False),
            scidownl_fn=MagicMock(return_value=None),
            config_path=str(tmp_path / "no.yaml"),
            policy_clearance_path=str(tmp_path / "no.json"),
        )

        attempts = get_acquisition_attempts_for_record(ref, db_path=db)
        blocked  = [a for a in attempts if a["outcome"] == "gate_blocked"]
        assert len(blocked) == 1
        assert blocked[0]["source"] == "scidownl"

    def test_transition_log_records_unpaywall_failure(self, tmp_path):
        db  = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        ref = _seed_accept_row(db)

        acquire_pdf_for_record(
            {"reference_id": ref, "doi": VALID_DOI, "title_raw": "T", "phase4d_decision": "ACCEPT"},
            db_path=db,
            pdf_dir=str(tmp_path / "pdfs"),
            unpaywall_client=self._make_up(False),
            openalex_client=self._make_up(False),
            config_path=str(tmp_path / "no.yaml"),
            policy_clearance_path=str(tmp_path / "no.json"),
        )

        attempts = get_acquisition_attempts_for_record(ref, db_path=db)
        sources  = {a["source"]: a["outcome"] for a in attempts}
        assert sources.get("unpaywall") == "failure"
        assert sources.get("openalex_oa") == "failure"

    def test_scidownl_called_when_all_four_pass(self, tmp_path):
        db  = str(tmp_path / "lc.db"); init_lifecycle_db(db)
        ref = _seed_accept_row(db)

        cfg = str(tmp_path / "cfg.yaml"); _write_config(cfg, armed=True)
        clr = str(tmp_path / "pc.json");  _write_clearance(clr)
        # Pre-log failures so C3 passes
        _log_both_failures(db, ref)

        sci_fn = MagicMock(return_value=None)  # returns None (not found)

        acquire_pdf_for_record(
            {"reference_id": ref, "doi": VALID_DOI, "title_raw": "T", "phase4d_decision": "ACCEPT"},
            db_path=db,
            pdf_dir=str(tmp_path / "pdfs"),
            unpaywall_client=self._make_up(False),
            openalex_client=self._make_up(False),
            scidownl_fn=sci_fn,
            config_path=cfg,
            policy_clearance_path=clr,
        )

        sci_fn.assert_called_once()


# ===========================================================================
# BACKWARD COMPAT: Phase5BPolicyGateError alias
# ===========================================================================

class TestBackwardCompat:
    def test_alias_is_same_class(self):
        from pdf_acquisition_engine import Phase5BPolicyGateError
        assert Phase5BPolicyGateError is ScidownlClearanceError

    def test_all_subtypes_catchable_as_alias(self):
        try:
            raise ConfigNotArmedError("REF-001", "test")
        except Phase5BPolicyGateError:
            pass  # must be caught
