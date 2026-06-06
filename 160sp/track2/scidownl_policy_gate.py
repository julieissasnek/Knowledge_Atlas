"""
scidownl_policy_gate.py -- Phase 5B: Four-condition scidownl clearance gate.

Every scidownl call in the pipeline MUST pass through verify_scidownl_clearance()
before execution.  The function checks all four conditions atomically and raises
a typed exception on the FIRST failure encountered.  A return value of True means
all four conditions passed simultaneously -- partial passes are not possible.

CLEARANCE CONDITIONS (all four must be True simultaneously):
  C1. YAML CONFIG ARMED
      acquisition_config.yaml must exist and contain
      enable_paid_or_grey_sources: true.
      If the file is missing, malformed, the key is absent, or the value is
      anything other than boolean true, ConfigNotArmedError is raised.

  C2. PHYSICAL POLICY COUNTERSIGNATURE
      policy_clearance.json must physically exist at the project root path
      passed to verify_scidownl_clearance().
      os.path.exists() is called explicitly -- symlinks, empty files, and
      unreadable files all count as present (existence is the unlock).
      PolicyClearanceMissingError is raised if absent.

  C3. CASCADE EXHAUSTION LOGGED
      The lifecycle_transitions table in pipeline_lifecycle_full.db must
      contain at least one row for this reference_id with source='unpaywall'
      AND at least one row with source='openalex_oa', both with outcome='failure'.
      If either source is missing from the log, CascadeNotExhaustedError is raised.
      This enforces the architectural invariant that scidownl is only ever a
      last resort -- not a first-try shortcut.

  C4. EXCLUSIVE TRIAGE ELEVATION
      The candidate row's phase4d_decision must be the exact string 'ACCEPT'.
      EDGE_CASE, REJECT, MISSING_ABSTRACT, and any other value raise
      TriageElevationError immediately.

EXCEPTION HIERARCHY:
  ScidownlClearanceError (base)
    ConfigNotArmedError         -- C1 failure
    PolicyClearanceMissingError -- C2 failure
    CascadeNotExhaustedError    -- C3 failure
    TriageElevationError        -- C4 failure

Each exception carries the reference_id and a human-readable message identifying
exactly which condition failed and what was observed vs. expected.

YAML PARSING:
  Requires PyYAML (pip install pyyaml).
  Falls back to a minimal regex parser for the single key we need if PyYAML
  is not installed, so the gate works even in stripped environments.

Usage:
    from scidownl_policy_gate import verify_scidownl_clearance

    try:
        verify_scidownl_clearance(
            reference_id   = row["reference_id"],
            phase4d_decision = row["phase4d_decision"],
            db_path        = db_path,
            config_path    = "/path/to/acquisition_config.yaml",
            policy_clearance_path = "/path/to/policy_clearance.json",
        )
    except ScidownlClearanceError as exc:
        log_transition(reference_id, "scidownl", "gate_blocked", ...)
        continue   # skip to next record
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

from lifecycle_db import (
    LIFECYCLE_DB,
    get_acquisition_attempts_for_record,
)

# ── Exception hierarchy ───────────────────────────────────────────────────────

class ScidownlClearanceError(RuntimeError):
    """Base class for all scidownl policy gate failures."""
    condition: int = 0

    def __init__(self, reference_id: str, message: str) -> None:
        self.reference_id = reference_id
        super().__init__(f"[C{self.condition}] {reference_id}: {message}")


class ConfigNotArmedError(ScidownlClearanceError):
    """C1: acquisition_config.yaml missing or enable_paid_or_grey_sources != true."""
    condition = 1


class PolicyClearanceMissingError(ScidownlClearanceError):
    """C2: policy_clearance.json not found at project root."""
    condition = 2


class CascadeNotExhaustedError(ScidownlClearanceError):
    """C3: Unpaywall and/or OpenAlex OA not yet attempted and logged as failed."""
    condition = 3


class TriageElevationError(ScidownlClearanceError):
    """C4: phase4d_decision is not strictly 'ACCEPT'."""
    condition = 4


# ── YAML parser ───────────────────────────────────────────────────────────────

def _load_yaml_key(config_path: str, key: str) -> Optional[object]:
    """
    Return the value of `key` from a YAML file.

    Tries PyYAML first; falls back to a single-key regex parser so the gate
    works even when PyYAML is not installed.

    Returns None if the file does not exist, the key is absent, or parsing fails.
    """
    if not os.path.exists(config_path):
        return None

    raw = Path(config_path).read_text(encoding="utf-8")

    # Primary: PyYAML
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(raw) or {}
        return data.get(key)
    except ImportError:
        pass
    except Exception:
        return None

    # Fallback: minimal regex for `key: value` on its own line
    # Handles: true/false/True/False/yes/no/1/0
    pattern = rf"^\s*{re.escape(key)}\s*:\s*(.+?)\s*(?:#.*)?$"
    m = re.search(pattern, raw, re.MULTILINE)
    if not m:
        return None
    raw_val = m.group(1).strip().lower()
    if raw_val in ("true", "yes", "1"):
        return True
    if raw_val in ("false", "no", "0"):
        return False
    return raw_val


# ── Individual condition checkers ─────────────────────────────────────────────

def _check_config_armed(reference_id: str, config_path: str) -> None:
    """
    C1: Parse acquisition_config.yaml and assert enable_paid_or_grey_sources == True.

    Raises ConfigNotArmedError if:
      - config_path does not exist
      - the key is missing from the file
      - the value is not boolean True (false, missing, commented out, or any string)
    """
    if not os.path.exists(config_path):
        raise ConfigNotArmedError(
            reference_id,
            f"acquisition_config.yaml not found at {config_path!r}. "
            "Create the file and set enable_paid_or_grey_sources: true to arm scidownl.",
        )

    value = _load_yaml_key(config_path, "enable_paid_or_grey_sources")

    if value is None:
        raise ConfigNotArmedError(
            reference_id,
            f"Key 'enable_paid_or_grey_sources' is absent from {config_path!r}. "
            "Add 'enable_paid_or_grey_sources: true' to arm scidownl.",
        )

    if value is not True:
        raise ConfigNotArmedError(
            reference_id,
            f"enable_paid_or_grey_sources = {value!r} in {config_path!r}. "
            "Must be boolean true (not 'true', 1, or any other value) to arm scidownl.",
        )


def _check_policy_clearance_file(reference_id: str, policy_clearance_path: str) -> None:
    """
    C2: Verify that policy_clearance.json physically exists at the project root.

    Uses os.path.exists() explicitly as required by the contract.
    Symlinks, empty files, and unreadable files all satisfy this condition --
    existence is the programmatic unlock.

    Raises PolicyClearanceMissingError if the file is absent.
    """
    if not os.path.exists(policy_clearance_path):
        raise PolicyClearanceMissingError(
            reference_id,
            f"policy_clearance.json not found at {policy_clearance_path}. "
            "Copy policy_clearance.json.template to policy_clearance.json, "
            "fill in all fields, and place it in the project root directory.",
        )


def _check_cascade_exhausted(
    reference_id: str,
    *,
    db_path: str,
) -> None:
    """
    C3: Verify that both 'unpaywall' and 'openalex_oa' were attempted and
    logged as failures in lifecycle_transitions for this reference_id.

    Raises CascadeNotExhaustedError with the name of whichever source is
    missing from the log.  Both must be present; partial exhaustion is not
    sufficient.
    """
    attempts = get_acquisition_attempts_for_record(reference_id, db_path=db_path)

    sources_failed = {
        row["source"]
        for row in attempts
        if row["outcome"] == "failure"
    }

    missing: list[str] = []
    if "unpaywall" not in sources_failed:
        missing.append("unpaywall")
    if "openalex_oa" not in sources_failed:
        missing.append("openalex_oa")

    if missing:
        raise CascadeNotExhaustedError(
            reference_id,
            f"Required cascade sources not yet logged as failed: {missing}. "
            f"Sources with failure entries so far: {sorted(sources_failed) or 'none'}. "
            "Both unpaywall and openalex_oa must be attempted and logged before "
            "scidownl is eligible.",
        )


def _check_triage_elevation(
    reference_id: str,
    phase4d_decision: str,
) -> None:
    """
    C4: Verify the record's phase4d_decision is strictly and exactly 'ACCEPT'.

    EDGE_CASE, REJECT, MISSING_ABSTRACT, empty string, None, and any other
    value immediately raise TriageElevationError.

    This check is enforced here at the gate itself regardless of any upstream
    filtering -- defense in depth.
    """
    if phase4d_decision != "ACCEPT":
        raise TriageElevationError(
            reference_id,
            f"phase4d_decision = {phase4d_decision!r}. Must be exactly 'ACCEPT'. "
            "EDGE_CASE, REJECT, and MISSING_ABSTRACT records are never eligible "
            "for scidownl under any policy configuration.",
        )


# ── Master gate function ──────────────────────────────────────────────────────

def verify_scidownl_clearance(
    reference_id: str,
    phase4d_decision: str,
    *,
    db_path: str = LIFECYCLE_DB,
    config_path: str,
    policy_clearance_path: str,
) -> bool:
    """
    Verify all four scidownl clearance conditions atomically.

    Conditions are checked in order C1 -> C2 -> C3 -> C4.
    The first failure raises the corresponding typed exception.
    Returns True only when all four conditions pass.

    Parameters
    ----------
    reference_id          : article_references.reference_id of the candidate
    phase4d_decision      : article_references.phase4d_decision for the candidate
    db_path               : pipeline_lifecycle_full.db path
    config_path           : absolute path to acquisition_config.yaml
    policy_clearance_path : absolute path to policy_clearance.json

    Raises
    ------
    ConfigNotArmedError         -- C1: YAML flag not set to true
    PolicyClearanceMissingError -- C2: countersignature file absent
    CascadeNotExhaustedError    -- C3: Unpaywall/OpenAlex not yet logged as failed
    TriageElevationError        -- C4: decision is not 'ACCEPT'
    """
    # C1: YAML config must be explicitly armed
    _check_config_armed(reference_id, config_path)

    # C2: policy_clearance.json must physically exist
    _check_policy_clearance_file(reference_id, policy_clearance_path)

    # C3: cascade exhaustion must be logged in lifecycle_transitions
    _check_cascade_exhausted(reference_id, db_path=db_path)

    # C4: triage decision must be exactly 'ACCEPT'
    _check_triage_elevation(reference_id, phase4d_decision)

    return True
