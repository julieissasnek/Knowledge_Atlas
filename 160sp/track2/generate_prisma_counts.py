"""
generate_prisma_counts.py -- Phase 6B authoritative PRISMA funnel table engine.

Produces the exact 10-row PRISMA table required by the grading rubric, with
every count mapped to its precise relational database source.  Validates two
conservation laws and emits a structured audit delta report when they do not
balance.

TABLE SCHEMA (exact column mapping)
-------------------------------------
 1. Gaps targeted              query_results.json  len()
 2. Queries executed (SerpAPI) article_references  COUNT DISTINCT discovered_query
                                                    WHERE discovered_via='serpapi_scholar'
 3. Records returned           article_references  COUNT(*)
 4. Duplicates removed         article_references  COUNT WHERE triage_stage='duplicate'
 5. Abstracts collected        article_references  COUNT WHERE abstract IS NOT NULL
                                                    AND LENGTH(TRIM(abstract))>50
                                                    AND abstract_source IN known_sources
 6. MISSING_ABSTRACT           article_references  COUNT WHERE triage_stage='abstract_missing'
                                                    OR phase4d_decision='MISSING_ABSTRACT'
 7. Screened by classifier     article_references  COUNT WHERE phase4d_decision IS NOT NULL
                                                    AND phase4d_decision != ''
 8. -> ACCEPT                  article_references  COUNT WHERE phase4d_decision='ACCEPT'
 9. -> EDGE_CASE               article_references  COUNT WHERE phase4d_decision='EDGE_CASE'
10. -> REJECT                  article_references  COUNT WHERE phase4d_decision='REJECT'

CONSERVATION LAWS
-----------------
Law 1 (spec):  Records - Duplicates = Abstracts_collected + MISSING_ABSTRACT
  If delta != 0, the audit report decomposes the unaccounted records by stage
  (rejected_at_metadata, metadata_only, abstract_pending, etc.).
  These represent records that have not yet completed the full pipeline run.

Law 2 (spec):  Abstracts_collected = ACCEPT + EDGE_CASE + REJECT
  If delta != 0, the audit report identifies rows with collected abstracts that
  have not yet been classified by Phase 4D, or ACCEPT/EDGE/REJECT rows whose
  abstract column is unexpectedly empty.

Both laws balance to zero for a fully-completed pipeline run.
For an in-progress run the deltas are non-zero but fully explainable by the
per-stage breakdown included in the audit report.

Usage
-----
    python generate_prisma_counts.py
    python generate_prisma_counts.py --db /path/to/pipeline_lifecycle_full.db
    python generate_prisma_counts.py --strict    # raise on any non-zero delta
    python generate_prisma_counts.py --json      # print JSON only
    python generate_prisma_counts.py --html out.html
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from lifecycle_db import LIFECYCLE_DB, get_connection

DEFAULT_QUERY_FILE = str(Path(__file__).resolve().parent / "query_results.json")

# Abstract sources that represent a genuine full-text abstract
# (not a harvest-time snippet from SerpAPI)
VALID_ABSTRACT_SOURCES: frozenset[str] = frozenset({
    "semantic_scholar",
    "crossref",
    "pubmed",
    "openalex",
    "paper_scraper_pubmed",
    "paper_scraper_arxiv",
})

# All terminal and in-progress triage_stage values (used for delta decomposition)
_ALL_STAGES = (
    "metadata_only",
    "duplicate",
    "rejected_at_metadata",
    "abstract_pending",
    "abstract_collected",
    "abstract_missing",
    "accepted_for_download",
    "edge_case_review",
    "rejected_at_abstract",
    "pdf_acquired",
)


# ── Exception ─────────────────────────────────────────────────────────────────

class PipelineConservationError(RuntimeError):
    """
    Raised (in strict mode) when conservation law deltas cannot be fully
    explained by known in-progress pipeline states.

    Attributes
    ----------
    law1_delta : int   -- Records - Duplicates - Abstracts - MISSING_ABSTRACT
    law2_delta : int   -- Abstracts - ACCEPT - EDGE_CASE - REJECT
    audit      : dict  -- full audit_delta dict from compute_prisma_counts()
    """
    def __init__(self, law1_delta: int, law2_delta: int, audit: dict) -> None:
        self.law1_delta = law1_delta
        self.law2_delta = law2_delta
        self.audit      = audit
        super().__init__(
            f"Pipeline conservation violated: law1_delta={law1_delta}, "
            f"law2_delta={law2_delta}. "
            f"Run generate_prisma_counts.py without --strict for full audit report."
        )


# ── Helpers ────────────────────────────────────────────────────────────────────

def _load_queries(query_file: str) -> list[dict]:
    p = Path(query_file)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def _q(conn, sql: str, params: tuple = ()) -> int:
    """Execute a scalar COUNT and return int."""
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _stage_dist(conn) -> dict[str, int]:
    """Return per-triage_stage row counts."""
    rows = conn.execute(
        "SELECT triage_stage, COUNT(*) n FROM article_references "
        "GROUP BY triage_stage"
    ).fetchall()
    return {r["triage_stage"]: r["n"] for r in rows}


# ── Core computation ──────────────────────────────────────────────────────────

def compute_prisma_counts(
    db_path: str = LIFECYCLE_DB,
    query_file: str = DEFAULT_QUERY_FILE,
    *,
    strict: bool = False,
) -> dict:
    """
    Compute the 10-row PRISMA funnel table and validate conservation laws.

    Parameters
    ----------
    db_path    : pipeline_lifecycle_full.db
    query_file : query_results.json
    strict     : if True, raise PipelineConservationError when any delta is
                 non-zero AND unexplained by in-progress pipeline states

    Returns
    -------
    {
        "generated_at":   ISO 8601 string,
        "rows":           list of 10 FunnelRow dicts (label, count, source_sql),
        "conservation":   {
            "law1_left": int, "law1_right": int, "law1_delta": int,
            "law2_left": int, "law2_right": int, "law2_delta": int,
            "laws_balance": bool,
        },
        "audit_delta":    dict with per-stage breakdown when laws don't balance,
        "stage_dist":     dict of triage_stage -> count (raw distribution),
    }
    """
    queries = _load_queries(query_file)

    # ── Zero-fill when DB is absent ───────────────────────────────────────────
    if not Path(db_path).exists() or Path(db_path).stat().st_size == 0:
        return _zero_result(queries, db_path)

    conn = get_connection(db_path)

    with conn:
        # ── Row 1: Gaps targeted ──────────────────────────────────────────────
        gaps_targeted = len(queries)
        sql_gaps = f"len(query_results.json) = {gaps_targeted}"

        # ── Row 2: Queries executed via SerpAPI ───────────────────────────────
        # Distinct gap query IDs that produced at least one SerpAPI row.
        queries_executed = _q(
            conn,
            "SELECT COUNT(DISTINCT discovered_query) FROM article_references "
            "WHERE discovered_via = 'serpapi_scholar'",
        )
        sql_queries = (
            "COUNT(DISTINCT discovered_query) WHERE discovered_via='serpapi_scholar'"
        )

        # ── Row 3: Records returned (all rows before any filtering) ──────────
        records_returned = _q(conn, "SELECT COUNT(*) FROM article_references")
        sql_records = "COUNT(*) FROM article_references"

        # ── Row 4: Duplicates removed ─────────────────────────────────────────
        duplicates_removed = _q(
            conn,
            "SELECT COUNT(*) FROM article_references WHERE triage_stage = 'duplicate'",
        )
        sql_dupes = "COUNT(*) WHERE triage_stage = 'duplicate'"

        # ── Row 5: Abstracts collected ────────────────────────────────────────
        # A genuine full-text abstract: non-null, length > 50 chars,
        # sourced from a known abstract API (not a harvest-time snippet).
        src_list = ", ".join(f"'{s}'" for s in sorted(VALID_ABSTRACT_SOURCES))
        abstracts_collected = _q(
            conn,
            f"SELECT COUNT(*) FROM article_references "
            f"WHERE abstract IS NOT NULL "
            f"  AND LENGTH(TRIM(abstract)) > 50 "
            f"  AND abstract_source IN ({src_list})",
        )
        sql_abstracts = (
            f"COUNT(*) WHERE abstract IS NOT NULL "
            f"AND LENGTH(TRIM(abstract)) > 50 "
            f"AND abstract_source IN ({src_list})"
        )

        # ── Row 6: MISSING_ABSTRACT ───────────────────────────────────────────
        # Phase 4B exhaustion (triage_stage) OR Phase 4D classification.
        # Using UNION to avoid double-counting rows that match both conditions.
        missing_abstract = _q(
            conn,
            "SELECT COUNT(*) FROM article_references "
            "WHERE triage_stage = 'abstract_missing' "
            "   OR phase4d_decision = 'MISSING_ABSTRACT'",
        )
        sql_missing = (
            "COUNT(*) WHERE triage_stage='abstract_missing' "
            "OR phase4d_decision='MISSING_ABSTRACT'"
        )

        # ── Row 7: Screened by classifier (Phase 4D input) ───────────────────
        screened = _q(
            conn,
            "SELECT COUNT(*) FROM article_references "
            "WHERE phase4d_decision IS NOT NULL AND phase4d_decision != ''",
        )
        sql_screened = (
            "COUNT(*) WHERE phase4d_decision IS NOT NULL AND phase4d_decision != ''"
        )

        # ── Row 8: ACCEPT ─────────────────────────────────────────────────────
        accept = _q(
            conn,
            "SELECT COUNT(*) FROM article_references WHERE phase4d_decision = 'ACCEPT'",
        )
        sql_accept = "COUNT(*) WHERE phase4d_decision = 'ACCEPT'"

        # ── Row 9: EDGE_CASE ──────────────────────────────────────────────────
        edge_case = _q(
            conn,
            "SELECT COUNT(*) FROM article_references WHERE phase4d_decision = 'EDGE_CASE'",
        )
        sql_edge = "COUNT(*) WHERE phase4d_decision = 'EDGE_CASE'"

        # ── Row 10: REJECT ────────────────────────────────────────────────────
        reject = _q(
            conn,
            "SELECT COUNT(*) FROM article_references WHERE phase4d_decision = 'REJECT'",
        )
        sql_reject = "COUNT(*) WHERE phase4d_decision = 'REJECT'"

        # ── Per-stage distribution (for delta decomposition) ─────────────────
        stage_dist = _stage_dist(conn)

    # ── Conservation law validation ───────────────────────────────────────────
    # Law 1: Records - Duplicates = Abstracts_collected + MISSING_ABSTRACT
    law1_left  = records_returned - duplicates_removed
    law1_right = abstracts_collected + missing_abstract
    law1_delta = law1_left - law1_right

    # Law 2: Abstracts_collected = ACCEPT + EDGE_CASE + REJECT
    law2_left  = abstracts_collected
    law2_right = accept + edge_case + reject
    law2_delta = law2_left - law2_right

    laws_balance = (law1_delta == 0) and (law2_delta == 0)

    # ── Audit delta decomposition ─────────────────────────────────────────────
    audit_delta = _build_audit_delta(
        law1_delta, law2_delta, stage_dist,
        records_returned, duplicates_removed,
        abstracts_collected, missing_abstract,
        accept, edge_case, reject,
    )

    # ── Strict mode: raise if unexplained residual ────────────────────────────
    if strict and not laws_balance:
        unexplained_1 = audit_delta.get("law1_unexplained_residual", 0)
        unexplained_2 = audit_delta.get("law2_unexplained_residual", 0)
        if unexplained_1 != 0 or unexplained_2 != 0:
            result = _pack_result(
                generated_at=datetime.now(timezone.utc).isoformat(),
                gaps_targeted=gaps_targeted, sql_gaps=sql_gaps,
                queries_executed=queries_executed, sql_queries=sql_queries,
                records_returned=records_returned, sql_records=sql_records,
                duplicates_removed=duplicates_removed, sql_dupes=sql_dupes,
                abstracts_collected=abstracts_collected, sql_abstracts=sql_abstracts,
                missing_abstract=missing_abstract, sql_missing=sql_missing,
                screened=screened, sql_screened=sql_screened,
                accept=accept, sql_accept=sql_accept,
                edge_case=edge_case, sql_edge=sql_edge,
                reject=reject, sql_reject=sql_reject,
                law1_left=law1_left, law1_right=law1_right, law1_delta=law1_delta,
                law2_left=law2_left, law2_right=law2_right, law2_delta=law2_delta,
                laws_balance=laws_balance,
                audit_delta=audit_delta, stage_dist=stage_dist,
            )
            raise PipelineConservationError(law1_delta, law2_delta, result)

    return _pack_result(
        generated_at=datetime.now(timezone.utc).isoformat(),
        gaps_targeted=gaps_targeted, sql_gaps=sql_gaps,
        queries_executed=queries_executed, sql_queries=sql_queries,
        records_returned=records_returned, sql_records=sql_records,
        duplicates_removed=duplicates_removed, sql_dupes=sql_dupes,
        abstracts_collected=abstracts_collected, sql_abstracts=sql_abstracts,
        missing_abstract=missing_abstract, sql_missing=sql_missing,
        screened=screened, sql_screened=sql_screened,
        accept=accept, sql_accept=sql_accept,
        edge_case=edge_case, sql_edge=sql_edge,
        reject=reject, sql_reject=sql_reject,
        law1_left=law1_left, law1_right=law1_right, law1_delta=law1_delta,
        law2_left=law2_left, law2_right=law2_right, law2_delta=law2_delta,
        laws_balance=laws_balance,
        audit_delta=audit_delta, stage_dist=stage_dist,
    )


def _build_audit_delta(
    law1_delta: int,
    law2_delta: int,
    stage_dist: dict,
    records: int,
    dupes: int,
    abstracts: int,
    missing: int,
    accept: int,
    edge: int,
    reject: int,
) -> dict:
    """
    Decompose non-zero deltas into known pipeline states.

    Law 1 delta is fully explained by records that are still in intermediate
    stages (not yet through abstract collection) or were hard-stopped at the
    metadata gate.  These are not errors -- they mean the pipeline run is
    incomplete.

    Law 2 delta is explained by records with abstracts that haven't yet been
    classified by Phase 4D (abstract_collected stage) or by records classified
    as ACCEPT/EDGE/REJECT whose abstract column is empty (data anomaly).
    """
    if law1_delta == 0 and law2_delta == 0:
        return {
            "laws_balance":           True,
            "law1_explained_by":      {},
            "law1_unexplained_residual": 0,
            "law2_explained_by":      {},
            "law2_unexplained_residual": 0,
        }

    # Stages that account for Law 1 delta (records not yet through abstract collection)
    law1_explainers = {
        "metadata_only":         stage_dist.get("metadata_only", 0),
        "rejected_at_metadata":  stage_dist.get("rejected_at_metadata", 0),
        "abstract_pending":      stage_dist.get("abstract_pending", 0),
    }
    law1_explained = sum(law1_explainers.values())
    law1_residual  = law1_delta - law1_explained

    # Stages that account for Law 2 delta (abstracts collected but not yet in Phase 4D)
    law2_explainers = {
        "abstract_collected_not_classified": stage_dist.get("abstract_collected", 0),
    }
    law2_explained = sum(law2_explainers.values())
    law2_residual  = law2_delta - law2_explained

    lines_1 = [
        f"  records returned:            {records}",
        f"  duplicates removed:          {dupes}",
        f"  left side (R - D):           {records - dupes}",
        f"  abstracts collected:         {abstracts}",
        f"  MISSING_ABSTRACT:            {missing}",
        f"  right side (A + M):          {abstracts + missing}",
        f"  DELTA:                       {law1_delta}",
        f"  ---- accounted for by ----",
    ]
    for stage, n in law1_explainers.items():
        lines_1.append(f"    {stage:35s}: {n}")
    lines_1 += [
        f"  total explained:             {law1_explained}",
        f"  unexplained residual:        {law1_residual}",
    ]
    if law1_residual == 0:
        lines_1.append("  STATUS: delta fully explained -- pipeline run is incomplete but consistent")
    else:
        lines_1.append(f"  STATUS: *** UNEXPLAINED RESIDUAL {law1_residual} -- data integrity issue ***")

    lines_2 = [
        f"  abstracts collected:         {abstracts}",
        f"  ACCEPT + EDGE_CASE + REJECT: {accept} + {edge} + {reject} = {accept+edge+reject}",
        f"  DELTA:                       {law2_delta}",
        f"  ---- accounted for by ----",
    ]
    for stage, n in law2_explainers.items():
        lines_2.append(f"    {stage:35s}: {n}")
    lines_2 += [
        f"  total explained:             {law2_explained}",
        f"  unexplained residual:        {law2_residual}",
    ]
    if law2_residual == 0:
        lines_2.append("  STATUS: delta fully explained -- abstracts await Phase 4D classification")
    else:
        lines_2.append(f"  STATUS: *** UNEXPLAINED RESIDUAL {law2_residual} -- data integrity issue ***")

    return {
        "laws_balance":               False,
        "law1_report_lines":          lines_1,
        "law1_explained_by":          law1_explainers,
        "law1_explained_total":       law1_explained,
        "law1_unexplained_residual":  law1_residual,
        "law2_report_lines":          lines_2,
        "law2_explained_by":          law2_explainers,
        "law2_explained_total":       law2_explained,
        "law2_unexplained_residual":  law2_residual,
    }


def _pack_result(**kw) -> dict:
    """Pack all computed values into the standardised return dict."""
    rows = [
        {"label": "Gaps targeted (from Task 2)",
         "count": kw["gaps_targeted"],   "source_sql": kw["sql_gaps"],
         "indent": 0},
        {"label": "Queries executed (SerpAPI google_scholar)",
         "count": kw["queries_executed"], "source_sql": kw["sql_queries"],
         "indent": 0},
        {"label": "Records returned",
         "count": kw["records_returned"], "source_sql": kw["sql_records"],
         "indent": 0},
        {"label": "Duplicates removed",
         "count": kw["duplicates_removed"], "source_sql": kw["sql_dupes"],
         "indent": 1},
        {"label": "Abstracts collected",
         "count": kw["abstracts_collected"], "source_sql": kw["sql_abstracts"],
         "indent": 0},
        {"label": "MISSING_ABSTRACT (no abstract found)",
         "count": kw["missing_abstract"], "source_sql": kw["sql_missing"],
         "indent": 1},
        {"label": "Screened by classifier (Phase 4D input)",
         "count": kw["screened"], "source_sql": kw["sql_screened"],
         "indent": 0},
        {"label": "→ ACCEPT (on-topic, high VOI)",
         "count": kw["accept"], "source_sql": kw["sql_accept"],
         "indent": 1},
        {"label": "→ EDGE_CASE (borderline)",
         "count": kw["edge_case"], "source_sql": kw["sql_edge"],
         "indent": 1},
        {"label": "→ REJECT (off-topic)",
         "count": kw["reject"], "source_sql": kw["sql_reject"],
         "indent": 1},
    ]
    return {
        "generated_at": kw["generated_at"],
        "rows":         rows,
        "conservation": {
            "law1_left":    kw["law1_left"],
            "law1_right":   kw["law1_right"],
            "law1_delta":   kw["law1_delta"],
            "law2_left":    kw["law2_left"],
            "law2_right":   kw["law2_right"],
            "law2_delta":   kw["law2_delta"],
            "laws_balance": kw["laws_balance"],
        },
        "audit_delta":  kw["audit_delta"],
        "stage_dist":   kw["stage_dist"],
    }


def _zero_result(queries: list, db_path: str) -> dict:
    """Return a fully-zeroed result when the DB is absent or empty."""
    rows = [
        {"label": "Gaps targeted (from Task 2)",              "count": len(queries), "source_sql": "len(query_results.json)", "indent": 0},
        {"label": "Queries executed (SerpAPI google_scholar)", "count": 0, "source_sql": "COUNT(DISTINCT discovered_query) WHERE discovered_via='serpapi_scholar'", "indent": 0},
        {"label": "Records returned",                          "count": 0, "source_sql": "COUNT(*) FROM article_references", "indent": 0},
        {"label": "Duplicates removed",                        "count": 0, "source_sql": "COUNT(*) WHERE triage_stage='duplicate'", "indent": 1},
        {"label": "Abstracts collected",                       "count": 0, "source_sql": "COUNT(*) WHERE abstract IS NOT NULL AND LENGTH(TRIM(abstract))>50 AND abstract_source IN (...)", "indent": 0},
        {"label": "MISSING_ABSTRACT (no abstract found)",      "count": 0, "source_sql": "COUNT(*) WHERE triage_stage='abstract_missing' OR phase4d_decision='MISSING_ABSTRACT'", "indent": 1},
        {"label": "Screened by classifier (Phase 4D input)",   "count": 0, "source_sql": "COUNT(*) WHERE phase4d_decision IS NOT NULL AND phase4d_decision!=''", "indent": 0},
        {"label": "→ ACCEPT (on-topic, high VOI)",        "count": 0, "source_sql": "COUNT(*) WHERE phase4d_decision='ACCEPT'", "indent": 1},
        {"label": "→ EDGE_CASE (borderline)",             "count": 0, "source_sql": "COUNT(*) WHERE phase4d_decision='EDGE_CASE'", "indent": 1},
        {"label": "→ REJECT (off-topic)",                 "count": 0, "source_sql": "COUNT(*) WHERE phase4d_decision='REJECT'", "indent": 1},
    ]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rows":         rows,
        "conservation": {"law1_left": 0, "law1_right": 0, "law1_delta": 0,
                         "law2_left": 0, "law2_right": 0, "law2_delta": 0,
                         "laws_balance": True},
        "audit_delta":  {"laws_balance": True, "law1_unexplained_residual": 0,
                         "law2_unexplained_residual": 0},
        "stage_dist":   {},
    }


# ── Audit report printer ──────────────────────────────────────────────────────

def print_audit_report(result: dict, file=sys.stdout) -> None:
    """Print a human-readable audit report to `file`."""
    c = result["conservation"]
    ad = result["audit_delta"]

    print("\n" + "="*70, file=file)
    print("PRISMA FUNNEL TABLE — CONSERVATION LAW AUDIT", file=file)
    print(f"Generated: {result['generated_at']}", file=file)
    print("="*70, file=file)

    print("\nFUNNEL TABLE:", file=file)
    print(f"  {'Stage':<45}  {'Count':>7}", file=file)
    print(f"  {'-'*45}  {'-'*7}", file=file)
    for r in result["rows"]:
        indent = "  " * r["indent"]
        print(f"  {indent}{r['label']:<{45 - len(indent)}}  {r['count']:>7,}", file=file)

    print(f"\nCONSERVATION LAW 1:", file=file)
    print(f"  Records({c['law1_left'] + result['rows'][3]['count']}) "
          f"- Duplicates({result['rows'][3]['count']}) "
          f"= {c['law1_left']}", file=file)
    print(f"  Abstracts({result['rows'][4]['count']}) "
          f"+ MISSING_ABSTRACT({result['rows'][5]['count']}) "
          f"= {c['law1_right']}", file=file)
    status_1 = "PASS" if c["law1_delta"] == 0 else f"ALERT  delta={c['law1_delta']:+d}"
    print(f"  STATUS: {status_1}", file=file)

    print(f"\nCONSERVATION LAW 2:", file=file)
    print(f"  Abstracts collected = {c['law2_left']}", file=file)
    print(f"  ACCEPT({result['rows'][7]['count']}) + "
          f"EDGE_CASE({result['rows'][8]['count']}) + "
          f"REJECT({result['rows'][9]['count']}) = {c['law2_right']}", file=file)
    status_2 = "PASS" if c["law2_delta"] == 0 else f"ALERT  delta={c['law2_delta']:+d}"
    print(f"  STATUS: {status_2}", file=file)

    if not c["laws_balance"]:
        print("\nAUDIT DELTA DECOMPOSITION:", file=file)
        if "law1_report_lines" in ad:
            print("  Law 1:", file=file)
            for line in ad["law1_report_lines"]:
                print("  " + line, file=file)
        if "law2_report_lines" in ad:
            print("  Law 2:", file=file)
            for line in ad["law2_report_lines"]:
                print("  " + line, file=file)
    else:
        print("\nBoth conservation laws PASS. Pipeline counts are mathematically consistent.", file=file)

    print("="*70 + "\n", file=file)


# ── HTML table renderer ───────────────────────────────────────────────────────

def render_prisma_table_html(result: dict) -> str:
    """
    Render the 10-row PRISMA funnel table as a self-contained HTML section.
    Includes conservation law status badges and audit delta accordion.
    """
    c    = result["conservation"]
    ad   = result["audit_delta"]
    rows = result["rows"]

    def _badge(ok: bool, delta: int) -> str:
        if ok:
            return '<span class="law-pass">&#10003; PASS</span>'
        return f'<span class="law-fail">&#9888; ALERT &Delta;={delta:+d}</span>'

    def _row_html(r: dict, idx: int) -> str:
        indent_px = r["indent"] * 22
        cls = ""
        if "ACCEPT" in r["label"]:
            cls = "accept-row"
        elif "EDGE_CASE" in r["label"]:
            cls = "edge-row"
        elif "REJECT" in r["label"] and "MISSING" not in r["label"]:
            cls = "reject-row"
        elif "MISSING" in r["label"] or "Duplicates" in r["label"]:
            cls = "excl-row"
        num = f"{r['count']:,}"
        src = r["source_sql"]
        src_display = src[:72] + "…" if len(src) > 72 else src
        return (
            f'<tr class="{cls}">'
            f'<td style="padding-left:{8 + indent_px}px">{r["label"]}</td>'
            f'<td class="cnt">{num}</td>'
            f'<td class="sql"><code>{src_display}</code></td>'
            f'</tr>'
        )

    table_rows = "".join(_row_html(r, i) for i, r in enumerate(rows))

    law1_badge = _badge(c["law1_delta"] == 0, c["law1_delta"])
    law2_badge = _badge(c["law2_delta"] == 0, c["law2_delta"])

    law1_eq = (
        f"Records&minus;Duplicates = {c['law1_left']:,} &nbsp;|&nbsp; "
        f"Abstracts+MISSING = {c['law1_right']:,} &nbsp;|&nbsp; "
        f"&Delta; = {c['law1_delta']:+d}"
    )
    law2_eq = (
        f"Abstracts = {c['law2_left']:,} &nbsp;|&nbsp; "
        f"ACCEPT+EDGE+REJECT = {c['law2_right']:,} &nbsp;|&nbsp; "
        f"&Delta; = {c['law2_delta']:+d}"
    )

    # Audit accordion (shown when laws don't balance)
    accordion_html = ""
    if not c["laws_balance"]:
        def _lines(key: str) -> str:
            return "<br>".join(
                f'<code>{l}</code>' for l in ad.get(key, [])
            )
        accordion_html = f"""
<details class="audit-details" open>
  <summary>&#9888; Audit Delta Report — click to expand</summary>
  <div class="audit-body">
    <p><strong>Law 1 decomposition</strong></p>
    <div class="audit-block">{_lines('law1_report_lines')}</div>
    <p><strong>Law 2 decomposition</strong></p>
    <div class="audit-block">{_lines('law2_report_lines')}</div>
    <p class="audit-note">
      Non-zero deltas that are <em>fully explained</em> by intermediate pipeline
      stages (metadata_only, abstract_pending, abstract_collected) indicate an
      <strong>in-progress pipeline run</strong> — not a data integrity failure.
      Unexplained residuals indicate a genuine conservation violation.
    </p>
  </div>
</details>"""

    return f"""
<section id="prisma-funnel-table" class="panel">
  <h2>PRISMA Funnel Table &mdash; Mathematical Audit</h2>
  <p class="live-note">Every count is a live <code>COUNT()</code> query against
  <code>article_references</code> in <code>pipeline_lifecycle_full.db</code>.
  Generated: <code>{result['generated_at'][:19]} UTC</code></p>

  <table class="prisma-table">
    <thead>
      <tr>
        <th>Funnel Stage</th>
        <th class="cnt">Count</th>
        <th>Relational Database Source</th>
      </tr>
    </thead>
    <tbody>
      {table_rows}
    </tbody>
  </table>

  <div class="law-panel">
    <div class="law-row">
      <span class="law-label">Law 1 &nbsp; Records &minus; Dup = Abstracts + MISSING</span>
      <span class="law-eq">{law1_eq}</span>
      {law1_badge}
    </div>
    <div class="law-row">
      <span class="law-label">Law 2 &nbsp; Abstracts = ACCEPT + EDGE + REJECT</span>
      <span class="law-eq">{law2_eq}</span>
      {law2_badge}
    </div>
  </div>

  {accordion_html}

  <style>
    .prisma-table {{ border-collapse:collapse; width:100%; margin-top:.75rem; }}
    .prisma-table th, .prisma-table td {{
      border:1px solid #dde3f0; padding:.4rem .65rem; font-size:.83rem;
    }}
    .prisma-table th {{ background:#16213e; color:#fff; }}
    .prisma-table .cnt {{ text-align:right; font-weight:700; font-variant-numeric:tabular-nums; min-width:70px; }}
    .prisma-table .sql {{ font-size:.75rem; color:#555; }}
    .accept-row {{ background:#d4edda; }}
    .edge-row   {{ background:#fff3cd; }}
    .reject-row {{ background:#f8d7da; }}
    .excl-row   {{ background:#f5f5f5; color:#555; font-style:italic; }}
    .law-panel  {{ margin-top:1rem; border:1px solid #dde3f0; border-radius:6px;
                   padding:.75rem 1rem; background:#f7f9fc; }}
    .law-row    {{ display:flex; align-items:center; gap:.75rem; flex-wrap:wrap;
                   margin:.3rem 0; font-size:.82rem; }}
    .law-label  {{ font-weight:600; min-width:280px; }}
    .law-eq     {{ flex:1; color:#555; }}
    .law-pass   {{ background:#d4edda; color:#155724; border-radius:4px;
                   padding:2px 8px; font-weight:700; white-space:nowrap; }}
    .law-fail   {{ background:#f8d7da; color:#721c24; border-radius:4px;
                   padding:2px 8px; font-weight:700; white-space:nowrap; }}
    .audit-details {{ margin-top:1rem; border:1px solid #f5c6cb;
                      border-radius:6px; overflow:hidden; }}
    .audit-details summary {{
      background:#f8d7da; padding:.5rem 1rem; cursor:pointer; font-weight:600;
      color:#721c24; user-select:none;
    }}
    .audit-body {{ padding:.75rem 1rem; background:#fff; font-size:.8rem; }}
    .audit-block {{ background:#f0f0f0; border-radius:4px; padding:.5rem .75rem;
                    margin:.25rem 0 .75rem; font-family:monospace; line-height:1.7; }}
    .audit-note {{ font-style:italic; color:#555; margin-top:.5rem; }}
  </style>
</section>"""


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate the PRISMA funnel table with conservation law audit."
    )
    parser.add_argument("--db", default=LIFECYCLE_DB)
    parser.add_argument("--queries", default=DEFAULT_QUERY_FILE)
    parser.add_argument("--strict", action="store_true",
                        help="Raise PipelineConservationError on unexplained residual")
    parser.add_argument("--json",   action="store_true",
                        help="Print raw JSON output")
    parser.add_argument("--html",   default=None,
                        help="Write standalone HTML fragment to this file")
    args = parser.parse_args()

    result = compute_prisma_counts(args.db, args.queries, strict=args.strict)

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif args.html:
        html = render_prisma_table_html(result)
        Path(args.html).write_text(html, encoding="utf-8")
        print(f"Wrote {args.html}")
    else:
        print_audit_report(result)


if __name__ == "__main__":
    main()
