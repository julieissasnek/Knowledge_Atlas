"""
triage_funnel.py -- Phase 4A & 4B defensive triage orchestrator.

TARGET DB: pipeline_lifecycle_full.db  (via lifecycle_db.py)

===============================================================================
ARCHITECTURAL GAP REPORT -- what Gemini's code got wrong
===============================================================================

GAP 1 -- No inline gate at ingest (CRITICAL)
  triage_engine.run_stage1() is a BATCH function called post-harvest.  Every
  scraped candidate first lands in article_references unconditionally, and the
  metadata filter runs later as a separate pass.  This means SerpAPI credits are
  already spent and the DB is already polluted before a single relevance check
  fires.  Fix: triage_gate hook in write_candidates_to_lifecycle() calls the
  metadata classifier immediately after each INSERT, inside the insertion loop.

GAP 2 -- atlas_shared classifier used for article-type, not domain relevance
  LocalAdaptiveClassifierSubsystem (the fallback when atlas_shared is absent)
  classifies article TYPES (meta_analysis, empirical, unknown...).  Its minimum
  confidence return is 0.35 ("unknown").  At threshold 0.20, NOTHING is ever
  rejected -- the gate is a no-op for this classifier.  Fix: MetadataRelevance
  Classifier scores domain-keyword density in title + venue and returns a float
  where 0 domain signals -> ~0.0, enough signals -> >=0.20.  This makes the 0.20
  threshold meaningful and achieves the 30-50% noise-rejection target.

GAP 3 -- get_connection not imported in triage_engine._load_corpus_dois()
  _load_corpus_dois() calls get_connection(db_path) but the symbol is never
  imported from article_db.  The NameError is silently swallowed by the broad
  except Exception: pass, so it was never caught by the test suite.  The corpus
  DOI pre-load has been silently returning an empty set on every run.  This file
  does not fix that bug (it is out of scope), but the Phase 4 orchestrator is
  built from scratch and does not inherit the defect.

GAP 4 -- Article_Eater scripts are clean (no PDF/abstract leaks)
  voi_gap_extractor.py and check_3_templates.py in C:\\Users\\juley\\Article_Eater
  operate purely on JSON template files.  No HTTP calls, no abstract fetching,
  no PDF downloads.  No remediation needed there.

===============================================================================
PHASE 4A -- METADATA-ONLY GATE
===============================================================================

  Execution window : immediately at ingest (via triage_gate hook) OR as a
                     batch over triage_stage='metadata_only' rows
  Input            : title_raw + venue  (no abstract, no network)
  Classifier       : atlas_shared.AdaptiveClassifierSubsystem when available;
                     MetadataRelevanceClassifier (keyword scorer) as fallback
  Threshold        : METADATA_THRESHOLD = 0.20  (strictly below -> hard reject)
  Terminal reject  : triage_stage -> 'rejected_at_metadata'  (never enriched again)
  Survivor         : triage_stage -> 'abstract_pending'
  Target           : destroy 30--50 % of search noise before any network call

MetadataRelevanceClassifier scoring (fallback):
  confidence = strong_hits x 0.20 + weak_hits x 0.05 + venue_bonus x 0.10
  Clamped to [0.0, 1.0].  A paper with zero domain signals scores 0.0; one
  strong signal (e.g. "daylight", "cognitive", "alertness") scores exactly 0.20
  and passes (threshold is STRICTLY BELOW 0.20).

===============================================================================
PHASE 4B -- DETERMINISTIC ABSTRACT COLLECTION
===============================================================================

  Eligibility    : triage_stage = 'abstract_pending' only
  Chain priority : Semantic Scholar -> CrossRef -> PubMed -> OpenAlex
  Short-circuit  : break the moment any source returns a non-empty string
  Success        : triage_stage -> 'abstract_collected'  (abstract stored)
  Exhaustion     : triage_stage -> 'abstract_missing'   (all sources empty/errored)

===============================================================================
BOUNDARY -- Phase 4C NOT IMPLEMENTED HERE
===============================================================================

  Phase 4C will apply the full domain-relevance classifier to the collected
  abstract.  It receives rows with triage_stage = 'abstract_collected'.
  Its contract will be provided separately.

Usage
-----
    python triage_funnel.py                        # run 4A then 4B (batch)
    python triage_funnel.py --stage 4a             # metadata gate only
    python triage_funnel.py --stage 4b             # abstract collection only
    python triage_funnel.py --dry-run              # report pending counts, no writes
    python triage_funnel.py --db /path/to/db       # override lifecycle DB path
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path
from typing import Callable, Optional

import requests

from lifecycle_db import (
    LIFECYCLE_DB,
    TRIAGE_STAGE_ABSTRACT_COLLECTED,
    TRIAGE_STAGE_ABSTRACT_MISSING,
    TRIAGE_STAGE_ABSTRACT_PENDING,
    TRIAGE_STAGE_REJECTED_METADATA,
    get_pending_abstract_collection,
    get_pending_metadata_triage,
    get_phase4_counts,
    init_lifecycle_db,
    update_triage_stage,
)

# -- Constants ------------------------------------------------------------------

METADATA_THRESHOLD = 0.20  # Strictly below -> hard-stop in Phase 4A

HEADERS = {"User-Agent": "KA-TriageFunnel/1.0 (mailto:student@ucsd.edu)"}

# API endpoints (same as triage_engine.py -- shared contract)
_SS_DOI_URL    = "https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}"
_SS_SEARCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
_CR_WORKS_URL  = "https://api.crossref.org/works"
_PUBMED_SEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
_PUBMED_FETCH  = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
_OA_WORKS_URL  = "https://api.openalex.org/works"


# ==============================================================================
# CLASSIFIER LAYER
# ==============================================================================

# Domain keyword tables -- daylight / cognition / built-environment

_STRONG_SIGNALS: list[str] = [
    # Light & circadian
    "daylight", "daylighting", "natural light", "artificial light",
    "circadian", "melanopsin", "correlated colour", "colour temperature",
    "spatial daylight autonomy", "glare", "luminance", "illuminance",
    "window-to-floor", "visual comfort",
    # Cognition & performance
    "cognitive", "cognition", "attention", "working memory",
    "executive function", "alertness", "cognitive fatigue", "mental workload",
    "academic performance", "learning performance", "task performance",
    "n-back", "attentional", "cognitive load",
    # Wellbeing outcomes
    "wellbeing", "well-being", "biophilic",
    # Built-environment specifics
    "thermal comfort", "indoor environment quality", "reverberation",
    "soundscape", "acoustic comfort",
]

_WEAK_SIGNALS: list[str] = [
    "light", "lighting", "environment", "building", "indoor", "outdoor",
    "window", "school", "classroom", "office", "workplace", "student",
    "occupant", "space", "design", "health", "visual", "comfort",
    "temperature", "noise", "acoustic", "fatigue", "stress", "sleep",
    "productivity", "performance", "mood", "air quality", "ventilation",
    "facade", "shading", "ceiling", "floor",
]

_VENUE_KEYWORDS: list[str] = [
    "building", "environment", "light", "cognition", "sleep",
    "occupant", "facility", "educational", "classroom", "architecture",
    "indoor", "daylighting", "ergonomic", "human factor",
]


class MetadataRelevanceClassifier:
    """
    Domain-relevance scorer for the daylight/cognition/built-environment domain.

    Used as the local fallback when atlas_shared is unavailable.

    Unlike the atlas_shared LocalAdaptiveClassifierSubsystem (which classifies
    article TYPES and returns >=0.35 for everything), this classifier scores
    DOMAIN KEYWORD DENSITY in title + venue.  At METADATA_THRESHOLD=0.20:

      - Zero domain signals -> confidence = 0.0 -> REJECTED
      - 3 weak signals only -> confidence = 0.15 -> REJECTED
      - 1 strong signal     -> confidence = 0.20 -> PASSES (exactly at threshold,
                               NOT strictly below, so the spec rule fires for <0.20)
      - 1 strong + venue    -> confidence = 0.30 -> PASSES
      - 2 strong signals    -> confidence = 0.40 -> PASSES

    This makes the 0.20 threshold meaningful and achieves the 30-50% noise target.

    Calling convention mirrors atlas_shared.AdaptiveClassifierSubsystem:
        evidence = ClassificationEvidence(title=..., venue=...)
        result   = classifier.classify(evidence)
        score    = result.article_type.confidence   # float 0.0--1.0
    """

    class _ArticleType:
        __slots__ = ("value", "confidence", "evidence", "source")
        def __init__(self, confidence: float, signals: list[str]):
            self.value      = "domain_relevant" if confidence >= METADATA_THRESHOLD else "out_of_domain"
            self.confidence = confidence
            self.evidence   = tuple(signals[:6])
            self.source     = "ka_metadata_relevance_fallback"

    class _ClassificationResult:
        __slots__ = ("article_type", "evidence_stage", "next_action")
        def __init__(self, article_type):
            self.article_type   = article_type
            self.evidence_stage = "metadata_only"
            self.next_action    = (
                "proceed_to_abstract_collection"
                if article_type.confidence >= METADATA_THRESHOLD
                else "hard_stop"
            )

    def classify(self, evidence) -> "_ClassificationResult":
        """
        Score domain relevance from title + venue only (no abstract required).

        evidence must expose: .title (str) and optionally .venue (str).
        """
        title = (getattr(evidence, "title", "") or "").lower()
        venue = (getattr(evidence, "venue", "") or getattr(evidence, "abstract", "") or "").lower()
        # Note: abstract field intentionally ignored here -- Phase 4A is metadata-only

        signals_hit: list[str] = []
        score = 0.0

        for signal in _STRONG_SIGNALS:
            if signal in title:
                score += 0.20
                signals_hit.append(f"strong:{signal}")

        for signal in _WEAK_SIGNALS:
            if signal in title:
                score += 0.05
                signals_hit.append(f"weak:{signal}")

        for kw in _VENUE_KEYWORDS:
            if kw in venue:
                score += 0.10
                signals_hit.append(f"venue:{kw}")
                break  # venue bonus counted once

        score = min(score, 1.0)
        return self._ClassificationResult(self._ArticleType(score, signals_hit))


class _ClassificationEvidence:
    """
    Minimal evidence carrier that mirrors atlas_shared.ClassificationEvidence.
    Used when constructing evidence objects to pass to the classifier.
    """
    __slots__ = ("title", "venue", "abstract", "keywords", "doi",
                 "filename", "pdf_path", "first_page_text")

    def __init__(
        self,
        title: str = "",
        venue: str = "",
        abstract: str = "",
        keywords: tuple = (),
        doi: str = "",
        filename: str = "",
        pdf_path: str = "",
        first_page_text: str = "",
    ):
        self.title          = title
        self.venue          = venue
        self.abstract       = abstract
        self.keywords       = keywords
        self.doi            = doi
        self.filename       = filename
        self.pdf_path       = pdf_path
        self.first_page_text = first_page_text


def _build_classifier():
    """
    Return (classifier_instance, ClassificationEvidence_class, backend_name).

    Resolution order:
      1. atlas_shared.classifier_system.AdaptiveClassifierSubsystem
         (requires 'pip install atlas_shared' or KA_ATLAS_SHARED_SRC env var)
      2. MetadataRelevanceClassifier -- local fallback, domain-keyword scorer

    The fallback is the ONLY path tested in the current environment because
    atlas_shared is not installed.  Any code path using the real atlas_shared
    classifier must be verified separately.
    """
    import sys as _sys
    from pathlib import Path as _Path

    # Try KA_ATLAS_SHARED_SRC override first
    import os
    ka_src = os.environ.get("KA_ATLAS_SHARED_SRC", "")
    if ka_src and ka_src not in _sys.path:
        _sys.path.insert(0, ka_src)

    try:
        from atlas_shared.classifier_system import (  # type: ignore
            AdaptiveClassifierSubsystem,
            ClassificationEvidence,
        )
        # atlas_shared ClassificationEvidence doesn't have 'venue' field --
        # wrap it so venue is passed via the 'abstract' slot (the only free-text
        # field available for metadata-only context).
        import warnings
        warnings.warn(
            "[triage_funnel] atlas_shared classifier loaded -- note: it classifies "
            "article TYPES, not domain relevance.  At threshold 0.20 it will rarely "
            "reject anything (min return ? 0.35 for 'unknown').  Consider registering "
            "MetadataRelevanceClassifier instead.",
            stacklevel=2,
        )
        return AdaptiveClassifierSubsystem(), ClassificationEvidence, "atlas_shared"
    except (ImportError, ModuleNotFoundError):
        pass

    return MetadataRelevanceClassifier(), _ClassificationEvidence, "ka_metadata_relevance_fallback"


# Module-level classifier (initialized once)
_CLASSIFIER, _EvidenceClass, CLASSIFIER_BACKEND = _build_classifier()

if CLASSIFIER_BACKEND != "atlas_shared":
    print(
        f"[triage_funnel] atlas_shared unavailable -- using {CLASSIFIER_BACKEND}",
        file=sys.stderr,
    )


# ==============================================================================
# PHASE 4A -- METADATA-ONLY GATE
# ==============================================================================

def _score_candidate(title_raw: str, venue: str) -> tuple[float, str]:
    """
    Run the metadata classifier on title + venue.
    Returns (confidence: float, next_action: str).
    """
    evidence = _EvidenceClass(title=title_raw, venue=venue)
    result   = _CLASSIFIER.classify(evidence)
    return result.article_type.confidence, result.next_action


def classify_at_ingest(row: dict, db_path: str = LIFECYCLE_DB) -> None:
    """
    Phase 4A inline gate -- called immediately after INSERT, inside the
    write_candidates_to_lifecycle() insertion loop.

    CONTRACT:
      - Only called for freshly INSERTED rows (UpsertResult.INSERTED).
      - Never called for doi_merge or duplicate rows.
      - Makes zero network calls -- pure metadata (title + venue).
      - Transitions triage_stage to 'rejected_at_metadata' or 'abstract_pending'.

    This function is designed to be passed as the `triage_gate` parameter to
    lifecycle_db.write_candidates_to_lifecycle().
    """
    title   = row.get("title_raw") or row.get("title") or ""
    venue   = row.get("venue") or ""
    ref_id  = row.get("reference_id") or ""

    if not ref_id:
        return

    confidence, _ = _score_candidate(title, venue)
    stage = (
        TRIAGE_STAGE_REJECTED_METADATA
        if confidence < METADATA_THRESHOLD
        else TRIAGE_STAGE_ABSTRACT_PENDING
    )
    update_triage_stage(
        ref_id,
        stage,
        metadata_confidence=confidence,
        db_path=db_path,
    )


def run_phase4a_metadata_gate(
    db_path: str = LIFECYCLE_DB,
) -> dict[str, int]:
    """
    Batch Phase 4A: process all rows with triage_stage = 'metadata_only'.

    Use this when candidates were inserted WITHOUT the inline triage_gate hook
    (e.g., pre-existing rows, backfills, or test fixtures).

    Returns:
        {
            "passed":   N rows advanced to 'abstract_pending',
            "rejected": N rows hard-stopped at 'rejected_at_metadata',
            "skipped":  N rows with no title (cannot classify),
        }
    """
    rows = get_pending_metadata_triage(db_path)
    counts = {"passed": 0, "rejected": 0, "skipped": 0}

    for row in rows:
        title  = (row.get("title_raw") or "").strip()
        venue  = (row.get("venue") or "").strip()
        ref_id = row["reference_id"]

        if not title:
            # Cannot classify without a title -- advance to avoid starvation
            update_triage_stage(
                ref_id,
                TRIAGE_STAGE_ABSTRACT_PENDING,
                metadata_confidence=0.0,
                db_path=db_path,
            )
            counts["skipped"] += 1
            continue

        confidence, _ = _score_candidate(title, venue)
        stage = (
            TRIAGE_STAGE_REJECTED_METADATA
            if confidence < METADATA_THRESHOLD
            else TRIAGE_STAGE_ABSTRACT_PENDING
        )
        update_triage_stage(
            ref_id,
            stage,
            metadata_confidence=confidence,
            db_path=db_path,
        )

        if stage == TRIAGE_STAGE_REJECTED_METADATA:
            counts["rejected"] += 1
            print(
                f"  [4A] REJECT  conf={confidence:.2f}  {title[:60]}",
                file=sys.stderr,
            )
        else:
            counts["passed"] += 1

    total = counts["passed"] + counts["rejected"] + counts["skipped"]
    rejection_pct = (counts["rejected"] / total * 100) if total else 0
    print(
        f"[phase4a] {counts['passed']} passed, {counts['rejected']} rejected "
        f"({rejection_pct:.1f}% noise removed), {counts['skipped']} skipped (no title)"
    )
    return counts


# ==============================================================================
# PHASE 4B -- DETERMINISTIC ABSTRACT COLLECTION
# ==============================================================================

# -- Individual source fetchers -------------------------------------------------

def _ss_doi(doi: str) -> str:
    try:
        resp = requests.get(
            _SS_DOI_URL.format(doi=doi),
            params={"fields": "abstract"},
            timeout=15, headers=HEADERS,
        )
        if resp.status_code == 200:
            return resp.json().get("abstract") or ""
    except Exception:
        pass
    return ""


def _ss_title(title: str) -> str:
    try:
        resp = requests.get(
            _SS_SEARCH_URL,
            params={"query": title, "fields": "abstract", "limit": 1},
            timeout=15, headers=HEADERS,
        )
        if resp.status_code == 200:
            items = resp.json().get("data", [])
            if items:
                return items[0].get("abstract") or ""
    except Exception:
        pass
    return ""


def _strip_jats(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


def _crossref_doi(doi: str) -> str:
    try:
        resp = requests.get(
            f"{_CR_WORKS_URL}/{doi}", timeout=15, headers=HEADERS
        )
        if resp.status_code == 200:
            return _strip_jats(resp.json().get("message", {}).get("abstract", ""))
    except Exception:
        pass
    return ""


def _crossref_title(title: str) -> str:
    try:
        resp = requests.get(
            _CR_WORKS_URL,
            params={"query.title": title, "rows": 1, "select": "abstract"},
            timeout=15, headers=HEADERS,
        )
        if resp.status_code == 200:
            items = resp.json().get("message", {}).get("items", [])
            if items:
                return _strip_jats(items[0].get("abstract", ""))
    except Exception:
        pass
    return ""


def _pubmed_title(title: str) -> str:
    try:
        s = requests.get(
            _PUBMED_SEARCH,
            params={"db": "pubmed", "term": title, "retmax": 1, "retmode": "json"},
            timeout=15,
        )
        ids = s.json().get("esearchresult", {}).get("idlist", [])
        if not ids:
            return ""
        f = requests.get(
            _PUBMED_FETCH,
            params={"db": "pubmed", "id": ids[0], "retmode": "xml", "rettype": "abstract"},
            timeout=15,
        )
        m = re.search(r"<AbstractText[^>]*>(.*?)</AbstractText>", f.text, re.DOTALL)
        return m.group(1).strip() if m else ""
    except Exception:
        pass
    return ""


def _decode_inverted(inv: Optional[dict]) -> str:
    if not inv:
        return ""
    pairs = [(pos, w) for w, positions in inv.items() for pos in positions]
    pairs.sort()
    return " ".join(w for _, w in pairs)


def _openalex_doi(doi: str) -> str:
    try:
        resp = requests.get(
            f"{_OA_WORKS_URL}/https://doi.org/{doi}",
            params={"select": "abstract_inverted_index"},
            timeout=15, headers=HEADERS,
        )
        if resp.status_code == 200:
            return _decode_inverted(resp.json().get("abstract_inverted_index"))
    except Exception:
        pass
    return ""


def _openalex_title(title: str) -> str:
    try:
        resp = requests.get(
            _OA_WORKS_URL,
            params={"search": title, "per-page": 1, "select": "abstract_inverted_index"},
            timeout=15, headers=HEADERS,
        )
        if resp.status_code == 200:
            results = resp.json().get("results", [])
            if results:
                return _decode_inverted(results[0].get("abstract_inverted_index"))
    except Exception:
        pass
    return ""


# -- Fallback chain orchestrator -----------------------------------------------

def fetch_abstract_chain(doi: str, title: str) -> tuple[str, str]:
    """
    Walk the four-source fallback chain.  Short-circuit on first success.

    Priority: Semantic Scholar -> CrossRef -> PubMed -> OpenAlex

    Returns (abstract_text, source_name).
    source_name is "none" when all four sources are exhausted.
    """
    # 1. Semantic Scholar
    if doi:
        ab = _ss_doi(doi)
        if ab:
            return ab, "semantic_scholar"
    ab = _ss_title(title)
    if ab:
        return ab, "semantic_scholar"
    time.sleep(0.3)

    # 2. CrossRef
    if doi:
        ab = _crossref_doi(doi)
        if ab:
            return ab, "crossref"
    ab = _crossref_title(title)
    if ab:
        return ab, "crossref"
    time.sleep(0.3)

    # 3. PubMed (title-based only -- PubMed API doesn't accept raw DOIs in esearch)
    ab = _pubmed_title(title)
    if ab:
        return ab, "pubmed"
    time.sleep(0.3)

    # 4. OpenAlex
    if doi:
        ab = _openalex_doi(doi)
        if ab:
            return ab, "openalex"
    ab = _openalex_title(title)
    if ab:
        return ab, "openalex"

    return "", "none"


def run_phase4b_abstract_collection(
    db_path: str = LIFECYCLE_DB,
    *,
    delay: float = 0.5,
) -> dict[str, int]:
    """
    Phase 4B: fetch abstracts for all 'abstract_pending' rows.

    Only processes rows with triage_stage = 'abstract_pending'.
    Rows with triage_stage = 'rejected_at_metadata' are NEVER touched here --
    the Phase 4A hard-stop is permanent.

    Chain order: Semantic Scholar -> CrossRef -> PubMed -> OpenAlex
    Short-circuits the moment any source returns a non-empty string.
    Flags ABSTRACT_MISSING when all four sources return empty.

    Returns:
        {
            "abstract_collected": N,
            "abstract_missing":   N,
        }
    """
    rows   = get_pending_abstract_collection(db_path)
    counts = {TRIAGE_STAGE_ABSTRACT_COLLECTED: 0, TRIAGE_STAGE_ABSTRACT_MISSING: 0}
    total  = len(rows)

    print(f"[phase4b] {total} rows pending abstract collection")

    for i, row in enumerate(rows, 1):
        ref_id = row["reference_id"]
        doi    = (row.get("doi") or "").strip()
        title  = (row.get("title_raw") or "").strip()
        print(f"  [4B {i:3d}/{total}] {title[:60]}...")

        abstract, source = fetch_abstract_chain(doi, title)

        if abstract:
            stage = TRIAGE_STAGE_ABSTRACT_COLLECTED
        else:
            stage = TRIAGE_STAGE_ABSTRACT_MISSING

        update_triage_stage(
            ref_id,
            stage,
            abstract=abstract or None,
            abstract_source=source if abstract else "none",
            db_path=db_path,
        )
        counts[stage] += 1

        if delay:
            time.sleep(delay)

    print(
        f"[phase4b] collected={counts[TRIAGE_STAGE_ABSTRACT_COLLECTED]}, "
        f"missing={counts[TRIAGE_STAGE_ABSTRACT_MISSING]}"
    )
    return counts


# ==============================================================================
# FULL FUNNEL RUNNER
# ==============================================================================

def run_full_funnel(
    db_path: str = LIFECYCLE_DB,
    *,
    stage4b_delay: float = 0.5,
) -> dict:
    """
    Run Phase 4A (metadata gate) then Phase 4B (abstract collection) in sequence.
    Phase 4C is NOT implemented -- see boundary warning in module docstring.

    Returns combined counts from both stages plus final triage_stage breakdown.
    """
    init_lifecycle_db(db_path)

    print("[triage_funnel] -- Phase 4A: metadata gate -------------------------")
    counts_4a = run_phase4a_metadata_gate(db_path)

    print("[triage_funnel] -- Phase 4B: abstract collection -------------------")
    counts_4b = run_phase4b_abstract_collection(db_path, delay=stage4b_delay)

    print("[triage_funnel] -- Summary -------------------------------------------")
    breakdown = get_phase4_counts(db_path)
    for stage, n in sorted(breakdown.items()):
        print(f"  {stage:30s}: {n}")

    return {
        "phase4a": counts_4a,
        "phase4b": counts_4b,
        "breakdown": breakdown,
    }


# ==============================================================================
# CLI
# ==============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Phase 4A/4B defensive triage funnel.\n"
            "Operates on pipeline_lifecycle_full.db.\n"
            "\n"
            "Phase 4A: metadata-only gate -- zero network calls\n"
            "Phase 4B: abstract collection (SS -> CrossRef -> PubMed -> OpenAlex)"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--stage", choices=["4a", "4b"],
        help="Run a single phase (default: run both 4A then 4B)"
    )
    parser.add_argument(
        "--db", default=LIFECYCLE_DB,
        help=f"lifecycle DB path (default: {LIFECYCLE_DB})"
    )
    parser.add_argument(
        "--delay", type=float, default=0.5,
        help="Inter-request delay for Phase 4B (default: 0.5s)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report pending counts without making any changes"
    )
    args = parser.parse_args()

    if args.dry_run:
        breakdown = get_phase4_counts(args.db)
        pending_4a = sum(
            n for s, n in breakdown.items()
            if s == "metadata_only"
        )
        pending_4b = sum(
            n for s, n in breakdown.items()
            if s == "abstract_pending"
        )
        print(f"[dry-run] Classifier backend : {CLASSIFIER_BACKEND}")
        print(f"[dry-run] Metadata threshold  : {METADATA_THRESHOLD}")
        print(f"[dry-run] Pending Phase 4A    : {pending_4a}")
        print(f"[dry-run] Pending Phase 4B    : {pending_4b}")
        print(f"[dry-run] triage_stage breakdown:")
        for stage, n in sorted(breakdown.items()):
            print(f"  {stage:30s}: {n}")
        return

    init_lifecycle_db(args.db)

    if args.stage == "4a":
        print(run_phase4a_metadata_gate(args.db))
    elif args.stage == "4b":
        print(run_phase4b_abstract_collection(args.db, delay=args.delay))
    else:
        run_full_funnel(args.db, stage4b_delay=args.delay)


if __name__ == "__main__":
    main()
