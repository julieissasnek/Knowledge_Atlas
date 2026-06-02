"""
prisma_export.py — Export live PRISMA funnel counts from article_references
and regenerate the PRISMA dashboard HTML (ka_topic_proposer.html).

This script is the authoritative source for PRISMA funnel numbers.
It reads directly from the SQLite database via get_prisma_counts() and
writes both:
  - prisma_counts.json    (machine-readable snapshot)
  - ka_topic_proposer.html (regenerated from the live counts template)

Running this after every triage_engine run keeps the dashboard in sync.

Usage
-----
    python prisma_export.py                           # regenerate from default DB
    python prisma_export.py --db article_references.db
    python prisma_export.py --counts-only             # print JSON, no HTML rewrite
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from article_db import DEFAULT_DB, get_all_records, get_prisma_counts, init_db

DEFAULT_COUNTS_FILE = "prisma_counts.json"
DEFAULT_HTML_FILE   = "ka_topic_proposer.html"


def export_counts(db_path: str = DEFAULT_DB, out_file: str = DEFAULT_COUNTS_FILE) -> dict:
    """Write prisma_counts.json and return the counts dict."""
    counts = get_prisma_counts(db_path)
    counts["generated_at"] = datetime.now(timezone.utc).isoformat()
    Path(out_file).write_text(
        json.dumps(counts, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"[prisma_export] Wrote {out_file}")
    return counts


def export_sample_table(db_path: str = DEFAULT_DB, n: int = 8) -> list[dict]:
    """Return the first n ACCEPT records for the sample table."""
    records = get_all_records(db_path)
    accepted = [r for r in records if r.get("final_decision") == "ACCEPT"]
    return accepted[:n]


def _badge(decision: str) -> str:
    cls = {
        "ACCEPT": "badge-accept",
        "EDGE_CASE": "badge-edge",
        "REJECT": "badge-reject",
        "MISSING_ABSTRACT": "badge-missing",
        "DUPLICATE": "badge-dup",
    }.get(decision, "")
    label = decision.replace("_", " ")
    return f'<span class="badge {cls}">{label}</span>'


def _sample_rows_html(records: list[dict]) -> str:
    rows = []
    for r in records:
        title = (r.get("title") or "")[:72] + ("…" if len(r.get("title", "")) > 72 else "")
        doi   = r.get("doi") or "(not found)"
        src   = r.get("abstract_source") or "—"
        dec   = r.get("final_decision") or "—"
        gap   = r.get("gap_id") or "—"
        rows.append(
            f"    <tr>"
            f"<td>{gap}</td>"
            f"<td>{title}</td>"
            f'<td><code>{doi}</code></td>'
            f"<td>{src}</td>"
            f"<td>{_badge(dec)}</td>"
            f"</tr>"
        )
    return "\n".join(rows)


def generate_html(counts: dict, sample_records: list[dict]) -> str:
    """Build the full PRISMA dashboard HTML from live counts."""
    identified   = counts.get("identified", 0)
    dupes        = counts.get("duplicates_removed", 0)
    screened     = counts.get("screened", 0)
    with_abs     = counts.get("with_abstract", 0)
    missing_abs  = counts.get("missing_abstract", 0)
    accept       = counts.get("accept", 0)
    edge_case    = counts.get("edge_case", 0)
    reject       = counts.get("reject", 0)
    pdfs         = counts.get("pdfs_retrieved", 0)
    generated_at = counts.get("generated_at", "")

    sample_html = _sample_rows_html(sample_records)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>PRISMA Funnel Dashboard — Knowledge Atlas Track 2</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 960px; margin: 2rem auto; padding: 0 1rem; color: #1a1a2e; }}
    h1  {{ color: #16213e; border-bottom: 3px solid #0f3460; padding-bottom: .5rem; }}
    h2  {{ color: #0f3460; margin-top: 2rem; }}
    .funnel {{ display: flex; flex-direction: column; align-items: center; gap: 0; margin: 2rem 0; }}
    .funnel-box {{
      background: #e8f4f8; border: 2px solid #0f3460; border-radius: 6px;
      padding: 1rem 2rem; text-align: center; position: relative;
      width: 100%; max-width: 580px; margin: 0 auto;
    }}
    .funnel-box .n     {{ font-size: 2rem; font-weight: 700; color: #0f3460; }}
    .funnel-box .label {{ font-size: 0.95rem; color: #333; }}
    .funnel-box.accept  {{ background: #d4edda; border-color: #28a745; }}
    .funnel-box.accept .n {{ color: #155724; }}
    .funnel-box.edge    {{ background: #fff3cd; border-color: #ffc107; }}
    .funnel-box.edge .n {{ color: #856404; }}
    .funnel-box.reject  {{ background: #f8d7da; border-color: #dc3545; }}
    .funnel-box.reject .n {{ color: #721c24; }}
    .arrow {{ font-size: 1.5rem; color: #666; text-align: center; line-height: 1.2; }}
    .excluded {{ font-size: 0.8rem; color: #666; font-style: italic; margin-top: 4px; }}
    table {{ border-collapse: collapse; width: 100%; margin-top: 1rem; }}
    th, td {{ border: 1px solid #ccc; padding: .5rem .75rem; text-align: left; font-size: .88rem; }}
    th {{ background: #16213e; color: #fff; }}
    tr:nth-child(even) {{ background: #f5f5f5; }}
    .badge {{ display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: .8rem; font-weight: 600; }}
    .badge-accept  {{ background: #d4edda; color: #155724; }}
    .badge-edge    {{ background: #fff3cd; color: #856404; }}
    .badge-reject  {{ background: #f8d7da; color: #721c24; }}
    .badge-missing {{ background: #e2e3e5; color: #383d41; }}
    .badge-dup     {{ background: #cce5ff; color: #004085; }}
    .summary-grid  {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(130px,1fr)); gap: .75rem; margin: 1.5rem 0; }}
    .summary-card  {{ border-radius: 8px; padding: .75rem; text-align: center; }}
    .summary-card .count {{ font-size: 1.8rem; font-weight: 700; }}
    .summary-card .label {{ font-size: .78rem; }}
    .live-note {{ font-size: .78rem; color: #888; font-style: italic; }}
    code {{ font-size: .85rem; background: #f0f0f0; padding: 1px 4px; border-radius: 3px; }}
  </style>
</head>
<body>

<h1>PRISMA Funnel Dashboard</h1>
<p>
  Track 2 &middot; Task 3 &mdash; Four-Scraper Harvest &amp; Three-Stage Triage<br/>
  <strong>Topic:</strong> Daylight &amp; Cognition / Built Environment<br/>
  <strong>Student:</strong> Julie Issasnek &nbsp;|&nbsp;
  <strong>Generated:</strong> {generated_at[:10] if generated_at else "2026-06-01"}
</p>
<p class="live-note">
  &dagger; All funnel counts are computed live from the
  <code>article_references</code> SQLite table via
  <code>article_db.get_prisma_counts()</code> &mdash; no hardcoded values.
  Regenerate with <code>python prisma_export.py</code>.
</p>

<h2>PRISMA-Style Identification &amp; Screening Flow</h2>

<div class="funnel">

  <div class="funnel-box">
    <div class="n">{identified}</div>
    <div class="label">Records <strong>identified</strong> via four-scraper harvest layer<br/>
      (SerpAPI &middot; scholarly &middot; paper-scraper &middot; 14 gap queries)</div>
  </div>

  <div class="arrow">&darr;</div>

  <div class="funnel-box">
    <div class="n">{screened}</div>
    <div class="label">Records <strong>screened</strong> after DOI deduplication</div>
    <div class="excluded">{dupes} duplicate{'' if dupes == 1 else 's'} removed</div>
  </div>

  <div class="arrow">&darr;</div>

  <div class="funnel-box">
    <div class="n">{with_abs}</div>
    <div class="label">Records with abstract retrieved<br/>
      (semantic_scholar &rarr; crossref &rarr; pubmed &rarr; openalex fallback chain)</div>
    <div class="excluded">{missing_abs} record{'' if missing_abs == 1 else 's'} flagged MISSING_ABSTRACT</div>
  </div>

  <div class="arrow">&darr; &nbsp; &darr; &nbsp; &darr;</div>

  <div style="display:flex;gap:1rem;width:100%;max-width:580px;justify-content:center">
    <div class="funnel-box accept" style="flex:1;width:auto;max-width:none">
      <div class="n">{accept}</div>
      <div class="label"><strong>ACCEPT</strong><br/>Strong domain signals (&ge;2)</div>
    </div>
    <div class="funnel-box edge" style="flex:1;width:auto;max-width:none">
      <div class="n">{edge_case}</div>
      <div class="label"><strong>EDGE CASE</strong><br/>Partial relevance</div>
    </div>
    <div class="funnel-box reject" style="flex:1;width:auto;max-width:none">
      <div class="n">{reject}</div>
      <div class="label"><strong>REJECT</strong><br/>Topic mismatch</div>
    </div>
  </div>

  <div class="arrow">&darr;</div>

  <div class="funnel-box accept" style="max-width:280px">
    <div class="n">{pdfs}</div>
    <div class="label">PDFs <strong>retrieved</strong> (Stage 3)<br/>
      <small>Unpaywall &rarr; scidownl (last resort)</small></div>
  </div>

</div>

<h2>Triage Decision Summary</h2>

<div class="summary-grid">
  <div class="summary-card" style="background:#d4edda">
    <div class="count" style="color:#155724">{accept}</div>
    <div class="label">ACCEPT</div>
  </div>
  <div class="summary-card" style="background:#fff3cd">
    <div class="count" style="color:#856404">{edge_case}</div>
    <div class="label">EDGE CASE</div>
  </div>
  <div class="summary-card" style="background:#f8d7da">
    <div class="count" style="color:#721c24">{reject}</div>
    <div class="label">REJECT</div>
  </div>
  <div class="summary-card" style="background:#e2e3e5">
    <div class="count" style="color:#383d41">{missing_abs}</div>
    <div class="label">MISSING ABSTRACT</div>
  </div>
  <div class="summary-card" style="background:#cce5ff">
    <div class="count" style="color:#004085">{dupes}</div>
    <div class="label">DUPLICATE</div>
  </div>
  <div class="summary-card" style="background:#e8f4f8">
    <div class="count" style="color:#0f3460">{pdfs}</div>
    <div class="label">PDFs RETRIEVED</div>
  </div>
</div>

<h2>Sample Accepted Records <span class="live-note">(from article_references)</span></h2>
<table>
  <thead>
    <tr>
      <th>Gap ID</th>
      <th>Title</th>
      <th>DOI</th>
      <th>Abstract Source</th>
      <th>Decision</th>
    </tr>
  </thead>
  <tbody>
{sample_html}
  </tbody>
</table>

<h2>Pipeline Architecture</h2>
<p>
  <code>gap_extractor.py</code>
  &rarr; <code>query_results.json</code>
  &rarr; <strong><code>harvest_layer.py</code></strong>
    (SerpAPI &middot; scholarly &middot; paper-scraper &mdash; scidownl reserved for Stage 3)
  &rarr; <code>article_references.db</code>
  &rarr; <strong><code>triage_engine.py</code></strong>
    (Stage&nbsp;1: metadata &middot; Stage&nbsp;2: abstract+classify &middot; Stage&nbsp;3: PDF gate)
  &rarr; <strong><code>prisma_export.py</code></strong>
    (live PRISMA counts from DB)
  &rarr; <code>af_handoff.json</code> (Article Finder boundary)
</p>

<h2>Decision Logic</h2>
<pre style="background:#f0f0f0;padding:1rem;border-radius:4px;font-size:.85rem;line-height:1.5">
Stage 1 (metadata-only — no network)
    title &lt; 5 chars?          &rarr; REJECT
    year outside [1990-2030]? &rarr; REJECT
    ALL-CAPS title?           &rarr; REJECT

Stage 2 (abstract + heuristic classifier)
    DOI already seen?         &rarr; DUPLICATE
    abstract &lt; 20 words?      &rarr; MISSING_ABSTRACT
    strong domain signals &ge;2? &rarr; ACCEPT
    strong &ge;1 OR weak &ge;3?    &rarr; EDGE_CASE
    otherwise                 &rarr; REJECT

Stage 3 (PDF — only for ACCEPT + EDGE_CASE)
    Unpaywall open-access URL &rarr; download
    Direct URL (.pdf)         &rarr; download
    scidownl (Sci-Hub)        &rarr; last resort
</pre>

</body>
</html>
"""


def regenerate_html(
    db_path: str = DEFAULT_DB,
    html_file: str = DEFAULT_HTML_FILE,
    counts_file: str = DEFAULT_COUNTS_FILE,
) -> dict:
    """Full export: write prisma_counts.json and regenerate ka_topic_proposer.html."""
    counts  = export_counts(db_path, counts_file)
    samples = export_sample_table(db_path)
    html    = generate_html(counts, samples)
    Path(html_file).write_text(html, encoding="utf-8")
    print(f"[prisma_export] Regenerated {html_file}")
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export PRISMA counts from article_references and regenerate dashboard HTML."
    )
    parser.add_argument("--db", default=DEFAULT_DB, help=f"DB path (default: {DEFAULT_DB})")
    parser.add_argument(
        "--html", default=DEFAULT_HTML_FILE,
        help=f"HTML output path (default: {DEFAULT_HTML_FILE})"
    )
    parser.add_argument(
        "--counts", default=DEFAULT_COUNTS_FILE,
        help=f"Counts JSON path (default: {DEFAULT_COUNTS_FILE})"
    )
    parser.add_argument(
        "--counts-only", action="store_true",
        help="Print counts JSON only, do not rewrite HTML"
    )
    args = parser.parse_args()

    init_db(args.db)

    if args.counts_only:
        import json as _json
        counts = get_prisma_counts(args.db)
        print(_json.dumps(counts, indent=2))
    else:
        counts = regenerate_html(args.db, args.html, args.counts)
        import json as _json
        print(_json.dumps(counts, indent=2))


if __name__ == "__main__":
    main()
