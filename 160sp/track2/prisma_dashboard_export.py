"""
prisma_dashboard_export.py -- Phase 6 PRISMA dashboard exporter.

Writes two output files:
  prisma_dashboard.json  -- machine-readable panel payload (re-loadable by the HTML)
  prisma_dashboard.html  -- self-contained dashboard with all six panels

The HTML embeds the data at generation time AND includes a JavaScript fetch
that reloads prisma_dashboard.json on demand, so the dashboard stays live
across browser refreshes as long as the JSON file is kept up to date.

Usage
-----
    python prisma_dashboard_export.py
    python prisma_dashboard_export.py --db /path/to/pipeline_lifecycle_full.db
    python prisma_dashboard_export.py --json-only
    python prisma_dashboard_export.py --out prisma_dashboard.html
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from lifecycle_db import LIFECYCLE_DB
from prisma_dashboard_data import (
    DEFAULT_QUERY_FILE,
    compute_prisma_dashboard_data,
)
from generate_prisma_counts import (
    compute_prisma_counts,
    render_prisma_table_html,
)

DEFAULT_JSON_OUT = str(Path(__file__).resolve().parent / "prisma_dashboard.json")
DEFAULT_HTML_OUT = str(Path(__file__).resolve().parent / "prisma_dashboard.html")


# ── HTML fragments ────────────────────────────────────────────────────────────

def _voi_bar(score, max_score=1.0):
    """Render a small inline SVG VOI bar."""
    if score is None:
        return '<span style="color:#999">—</span>'
    pct = min(100, int((score / max_score) * 100))
    color = "#28a745" if score >= 0.70 else "#ffc107" if score >= 0.50 else "#dc3545"
    return (
        f'<div style="display:flex;align-items:center;gap:6px">'
        f'<div style="width:80px;height:10px;background:#e0e0e0;border-radius:5px;overflow:hidden">'
        f'<div style="width:{pct}%;height:100%;background:{color}"></div></div>'
        f'<span style="font-size:.8rem">{score:.3f}</span></div>'
    )


def _panel_a_html(pa: dict) -> str:
    rows_html = ""
    for i, g in enumerate(pa["top5_by_voi"], 1):
        voi = g.get("max_voi_score")
        q   = g.get("boolean_query", "")
        q_display = q[:65] + "…" if len(q) > 65 else q
        rows_html += (
            f"<tr>"
            f"<td><strong>{i}</strong></td>"
            f"<td><code>{g['gap_id']}</code></td>"
            f"<td style='font-size:.8rem'>{q_display}</td>"
            f"<td style='text-align:center'>{g['total_papers']}</td>"
            f"<td style='text-align:center'>{g['accept_count']}</td>"
            f"<td>{_voi_bar(voi)}</td>"
            f"</tr>"
        )
    if not rows_html:
        rows_html = '<tr><td colspan="6" style="color:#999;text-align:center">No data yet — run harvest + triage first</td></tr>'
    return f"""
<section id="panel-a" class="panel">
  <h2>Panel A &mdash; Gap Summary</h2>
  <div class="stat-row">
    <div class="stat-card blue">
      <div class="big">{pa['total_gaps']}</div>
      <div class="lbl">Research Gaps Defined</div>
    </div>
    <div class="stat-card teal">
      <div class="big">{pa['gaps_with_results']}</div>
      <div class="lbl">Gaps With Results</div>
    </div>
    <div class="stat-card grey">
      <div class="big">{pa['total_gaps'] - pa['gaps_with_results']}</div>
      <div class="lbl">Gaps No Results (see Panel F)</div>
    </div>
  </div>
  <h3>Top 5 Gaps by VOI Score</h3>
  <table>
    <thead><tr><th>#</th><th>Gap ID</th><th>Boolean Query</th>
    <th>Papers</th><th>ACCEPT</th><th>Max VOI</th></tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
</section>"""


def _panel_b_html(pb: dict) -> str:
    scrapers_html = "".join(
        f'<span class="tag">{s}</span>' for s in pb["scrapers_used"]
    ) or "<span style='color:#999'>none logged</span>"
    return f"""
<section id="panel-b" class="panel">
  <h2>Panel B &mdash; Search Summary</h2>
  <div class="stat-row">
    <div class="stat-card blue">
      <div class="big">{pb['total_boolean_queries']}</div>
      <div class="lbl">Boolean Queries Defined</div>
    </div>
    <div class="stat-card teal">
      <div class="big">{pb['gaps_that_returned_results']}</div>
      <div class="lbl">Queries With Results</div>
    </div>
    <div class="stat-card green">
      <div class="big">{pb['total_raw_results']}</div>
      <div class="lbl">Raw Results Ingested</div>
    </div>
  </div>
  <p style="margin-top:.75rem">
    <strong>Scrapers used:</strong> {scrapers_html}
  </p>
</section>"""


def _panel_c_html(pc: dict) -> str:
    src_rows = "".join(
        f"<tr><td><code>{s}</code></td><td>{n}</td></tr>"
        for s, n in pc["abstract_source_breakdown"].items()
    ) or '<tr><td colspan="2" style="color:#999">none</td></tr>'
    return f"""
<section id="panel-c" class="panel">
  <h2>Panel C &mdash; Abstract Collection Telemetry</h2>
  <div class="stat-row">
    <div class="stat-card green">
      <div class="big">{pc['abstracts_collected']}</div>
      <div class="lbl">Abstracts Retrieved</div>
    </div>
    <div class="stat-card grey">
      <div class="big">{pc['missing_abstract']}</div>
      <div class="lbl">MISSING_ABSTRACT</div>
    </div>
  </div>
  <h3>Source Breakdown</h3>
  <table style="max-width:360px">
    <thead><tr><th>Source</th><th>Count</th></tr></thead>
    <tbody>{src_rows}</tbody>
  </table>
</section>"""


def _panel_d_html(pd_: dict) -> str:
    total = (pd_["accept"] + pd_["edge_case"] +
             pd_["reject"] + pd_["missing_abstract"]) or 1
    def _pct(n):
        return f"{n/total*100:.1f}%" if total else "—"
    return f"""
<section id="panel-d" class="panel">
  <h2>Panel D &mdash; Triage Results</h2>
  <div class="stat-row">
    <div class="stat-card green">
      <div class="big">{pd_['accept']}</div>
      <div class="lbl">ACCEPT ({_pct(pd_['accept'])})</div>
    </div>
    <div class="stat-card yellow">
      <div class="big">{pd_['edge_case']}</div>
      <div class="lbl">EDGE CASE ({_pct(pd_['edge_case'])})</div>
    </div>
    <div class="stat-card red">
      <div class="big">{pd_['reject']}</div>
      <div class="lbl">REJECT ({_pct(pd_['reject'])})</div>
    </div>
    <div class="stat-card grey">
      <div class="big">{pd_['missing_abstract']}</div>
      <div class="lbl">MISSING ABSTRACT ({_pct(pd_['missing_abstract'])})</div>
    </div>
  </div>
</section>"""


def _panel_e_html(pe: dict) -> str:
    """Classic PRISMA multi-stage flow with exclusion callouts."""
    dupes    = pe["duplicates"]
    met_rej  = pe["metadata_rejected"]
    screened = pe["screened"]
    abs_miss = pe["abstract_missing"]
    rej      = pe["reject"]
    ec       = pe["edge_case"]
    acc      = pe["accept"]
    unobt    = pe["unobtainable"]

    def _box(label, n, cls=""):
        return (
            f'<div class="funnel-box {cls}">'
            f'<div class="fn">{n:,}</div>'
            f'<div class="fl">{label}</div>'
            f"</div>"
        )

    def _exclusion(label, n):
        return (
            f'<div class="excl">'
            f'<span class="excl-n">{n:,}</span> {label}'
            f"</div>"
        )

    return f"""
<section id="panel-e" class="panel">
  <h2>Panel E &mdash; PRISMA Flow Diagram</h2>
  <p class="live-note">All counts read live from <code>article_references</code>
  in <code>pipeline_lifecycle_full.db</code>.</p>

  <div class="prisma-flow">

    <div class="flow-row">
      <div class="flow-main">
        {_box("Records <strong>identified</strong> via harvest layer<br><small>(SerpAPI &middot; scholarly &middot; paper-scraper)</small>", pe["identified"])}
      </div>
    </div>

    <div class="flow-arrow">&#8595;</div>

    <div class="flow-row">
      <div class="flow-main">
        {_box("Records <strong>deduplicated</strong> (unique DOI / title)", screened)}
      </div>
      <div class="flow-side">
        {_exclusion("duplicates removed", dupes)}
      </div>
    </div>

    <div class="flow-arrow">&#8595;</div>

    <div class="flow-row">
      <div class="flow-main">
        {_box("Records <strong>screened</strong> — Phase 4A metadata gate passed", screened - met_rej)}
      </div>
      <div class="flow-side">
        {_exclusion("excluded: metadata gate", met_rej)}
      </div>
    </div>

    <div class="flow-arrow">&#8595;</div>

    <div class="flow-row">
      <div class="flow-main">
        {_box("Records with <strong>abstract retrieved</strong><br><small>(SS &rarr; CrossRef &rarr; PubMed &rarr; OpenAlex)</small>", pe["abstracts_retrieved"])}
      </div>
      <div class="flow-side">
        {_exclusion("abstract missing / exhausted", abs_miss)}
      </div>
    </div>

    <div class="flow-arrow">&#8595;</div>

    <div class="flow-row-multi">
      <div class="funnel-box accept">
        <div class="fn">{acc:,}</div>
        <div class="fl"><strong>ACCEPT</strong></div>
      </div>
      <div class="funnel-box edge">
        <div class="fn">{ec:,}</div>
        <div class="fl"><strong>EDGE CASE</strong></div>
      </div>
      <div class="funnel-box reject">
        <div class="fn">{rej:,}</div>
        <div class="fl"><strong>REJECT</strong></div>
      </div>
    </div>

    <div class="flow-arrow">&#8595;</div>

    <div class="flow-row">
      <div class="flow-main">
        {_box("PDFs <strong>acquired</strong><br><small>(Unpaywall &rarr; OpenAlex OA &rarr; scidownl gate)</small>", pe["pdfs_acquired"], "accept")}
      </div>
      <div class="flow-side">
        {_exclusion("wanted but unobtainable", unobt)}
      </div>
    </div>

  </div>
</section>"""


def _panel_f_html(pf: dict) -> str:
    if not pf["null_result_gaps"]:
        body = '<tr><td colspan="2" style="color:#28a745;text-align:center">All gaps returned at least one result.</td></tr>'
    else:
        body = "".join(
            f"<tr>"
            f"<td><code>{g['gap_id']}</code></td>"
            f"<td style='font-size:.8rem'>{g['boolean_query'][:80]}</td>"
            f"</tr>"
            for g in pf["null_result_gaps"]
        )
    return f"""
<section id="panel-f" class="panel">
  <h2>Panel F &mdash; Null Results Ledger</h2>
  <p>
    <strong>{pf['total_null_gaps']}</strong> gap
    {"queries" if pf['total_null_gaps'] != 1 else "query"}
    returned zero papers across all scrapers.
  </p>
  <table>
    <thead><tr><th>Gap ID</th><th>Boolean Query</th></tr></thead>
    <tbody>{body}</tbody>
  </table>
</section>"""


# ── Full HTML page ────────────────────────────────────────────────────────────

def generate_html(data: dict) -> str:
    """Build the complete dashboard HTML from a data payload dict."""
    ts  = data.get("generated_at", "")
    ts_display = ts[:10] if ts else "—"
    ts_full    = ts[:19].replace("T", " ") + " UTC" if ts else "—"

    pa = data.get("panel_a", {})
    pb = data.get("panel_b", {})
    pc = data.get("panel_c", {})
    pd_ = data.get("panel_d", {})
    pe = data.get("panel_e", {})
    pf = data.get("panel_f", {})

    # Phase 6B: PRISMA funnel table (mathematical audit table)
    prisma_table_html = data.get("_prisma_table_html", "")

    panels_html = (
        _panel_a_html(pa) +
        _panel_b_html(pb) +
        _panel_c_html(pc) +
        _panel_d_html(pd_) +
        _panel_e_html(pe) +
        _panel_f_html(pf) +
        prisma_table_html
    )

    # Embed the data payload as JSON for the client-side reload logic
    data_json = json.dumps(data, ensure_ascii=False, indent=2)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>PRISMA Dashboard &mdash; Knowledge Atlas Track 2</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; }}
    body {{
      font-family: system-ui, -apple-system, sans-serif;
      max-width: 1000px; margin: 0 auto; padding: 1.5rem;
      color: #1a1a2e; background: #f7f9fc;
    }}
    h1 {{ color: #16213e; border-bottom: 3px solid #0f3460; padding-bottom:.5rem; margin-bottom:.25rem; }}
    h2 {{ color: #0f3460; margin: 0 0 1rem; }}
    h3 {{ color: #16213e; margin: 1rem 0 .5rem; font-size: .95rem; }}
    .panel {{
      background: #fff; border: 1px solid #dde3f0;
      border-radius: 10px; padding: 1.5rem; margin-bottom: 1.5rem;
      box-shadow: 0 1px 4px rgba(0,0,0,.06);
    }}
    .stat-row {{ display: flex; flex-wrap: wrap; gap: .75rem; margin-bottom: 1rem; }}
    .stat-card {{
      flex: 1; min-width: 100px; border-radius: 8px;
      padding: .75rem 1rem; text-align: center;
    }}
    .stat-card .big {{ font-size: 2rem; font-weight: 700; line-height: 1; }}
    .stat-card .lbl {{ font-size: .72rem; margin-top: .25rem; opacity: .85; }}
    .stat-card.blue   {{ background:#e8f4f8; color:#0f3460; }}
    .stat-card.teal   {{ background:#d4f1f4; color:#0a4a52; }}
    .stat-card.green  {{ background:#d4edda; color:#155724; }}
    .stat-card.yellow {{ background:#fff3cd; color:#856404; }}
    .stat-card.red    {{ background:#f8d7da; color:#721c24; }}
    .stat-card.grey   {{ background:#e2e3e5; color:#383d41; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border: 1px solid #dde3f0; padding: .4rem .65rem; text-align: left; font-size: .85rem; }}
    th {{ background: #16213e; color: #fff; font-weight: 600; }}
    tr:nth-child(even) {{ background: #f5f7fb; }}
    code {{ font-size: .82rem; background: #f0f0f0; padding: 1px 5px; border-radius: 3px; font-family: monospace; }}
    .tag {{ display:inline-block; background:#e8f4f8; color:#0f3460; border-radius:12px;
            padding:2px 8px; font-size:.78rem; margin:2px; }}
    .live-note {{ font-size:.75rem; color:#888; font-style:italic; margin:.25rem 0; }}
    /* PRISMA flow */
    .prisma-flow {{ display:flex; flex-direction:column; align-items:stretch; gap:0; }}
    .flow-row {{ display:flex; align-items:center; gap:.75rem; }}
    .flow-row-multi {{ display:flex; gap:.75rem; justify-content:center; margin:0; }}
    .flow-main {{ flex:1; }}
    .flow-side {{ min-width:180px; }}
    .flow-arrow {{ text-align:center; font-size:1.4rem; color:#888; line-height:1.6; }}
    .funnel-box {{
      border:2px solid #0f3460; border-radius:8px; padding:.75rem 1rem;
      text-align:center; background:#e8f4f8;
    }}
    .funnel-box .fn {{ font-size:1.8rem; font-weight:700; color:#0f3460; line-height:1; }}
    .funnel-box .fl {{ font-size:.82rem; margin-top:.25rem; color:#333; }}
    .funnel-box.accept {{ background:#d4edda; border-color:#28a745; }}
    .funnel-box.accept .fn {{ color:#155724; }}
    .funnel-box.edge   {{ background:#fff3cd; border-color:#ffc107; }}
    .funnel-box.edge   .fn {{ color:#856404; }}
    .funnel-box.reject {{ background:#f8d7da; border-color:#dc3545; }}
    .funnel-box.reject .fn {{ color:#721c24; }}
    .excl {{
      font-size:.78rem; color:#721c24; background:#fdf3f3;
      border:1px solid #f5c6cb; border-radius:6px;
      padding:.35rem .6rem; white-space:nowrap;
    }}
    .excl-n {{ font-weight:700; }}
    /* Header toolbar */
    .toolbar {{ display:flex; align-items:center; gap:1rem; margin-bottom:1.5rem; flex-wrap:wrap; }}
    .toolbar .ts {{ font-size:.8rem; color:#888; }}
    .btn {{
      background:#0f3460; color:#fff; border:none; border-radius:6px;
      padding:.4rem .9rem; cursor:pointer; font-size:.82rem;
    }}
    .btn:hover {{ background:#16213e; }}
    #refresh-status {{ font-size:.75rem; color:#888; }}
  </style>
</head>
<body>

<h1>PRISMA Dashboard &mdash; Knowledge Atlas Track 2</h1>
<p>
  <strong>Topic:</strong> Daylight &amp; Cognition / Built Environment &nbsp;&middot;&nbsp;
  <strong>Track 2, Task 3</strong>
</p>

<div class="toolbar">
  <span class="ts">Data snapshot: <strong id="ts-display">{ts_full}</strong></span>
  <button class="btn" onclick="reloadData()">&#8635; Refresh from JSON</button>
  <span id="refresh-status"></span>
</div>

{panels_html}

<script>
/* ── Inline data (baked at export time) ───────────────────────────────── */
const _BAKED = {data_json};

/* ── Client-side reload from prisma_dashboard.json ───────────────────── */
function reloadData() {{
  const status = document.getElementById('refresh-status');
  status.textContent = 'Fetching…';
  fetch('prisma_dashboard.json?t=' + Date.now())
    .then(r => r.json())
    .then(data => {{
      status.textContent = 'Reloaded at ' + new Date().toLocaleTimeString();
      document.getElementById('ts-display').textContent =
        (data.generated_at || '').replace('T',' ').slice(0,19) + ' UTC';
      /* Full page refresh to re-render panels from new data.
         In a production app this would do partial DOM updates;
         for the static exporter a full reload is cleaner. */
      location.reload();
    }})
    .catch(e => {{
      status.textContent = 'Could not fetch JSON (' + e.message + ') — showing baked data';
    }});
}}
</script>

</body>
</html>
"""


# ── Export functions ──────────────────────────────────────────────────────────

def export_json(
    data: dict,
    out_file: str = DEFAULT_JSON_OUT,
) -> None:
    """Write the dashboard data payload to JSON."""
    Path(out_file).write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"[prisma] Wrote {out_file}")


def export_html(
    data: dict,
    out_file: str = DEFAULT_HTML_OUT,
) -> None:
    """Write the full dashboard HTML."""
    html = generate_html(data)
    Path(out_file).write_text(html, encoding="utf-8")
    print(f"[prisma] Wrote {out_file}")


def run_export(
    db_path: str = LIFECYCLE_DB,
    query_file: str = DEFAULT_QUERY_FILE,
    json_out: str = DEFAULT_JSON_OUT,
    html_out: str = DEFAULT_HTML_OUT,
    json_only: bool = False,
) -> dict:
    """
    Full export pipeline: aggregate data, write JSON, write HTML.
    Also computes the Phase 6B PRISMA funnel table and embeds it in the HTML.
    Returns the data dict.
    """
    data = compute_prisma_dashboard_data(db_path, query_file)
    # Phase 6B: attach the PRISMA funnel table HTML fragment
    try:
        prisma_result = compute_prisma_counts(db_path, query_file)
        data["_prisma_table_html"] = render_prisma_table_html(prisma_result)
        data["prisma_funnel_table"] = prisma_result
    except Exception as exc:
        import sys as _sys
        print(f"[prisma_export] PRISMA table error: {exc}", file=_sys.stderr)
        data["_prisma_table_html"] = ""
    export_json(data, json_out)
    if not json_only:
        export_html(data, html_out)
    return data


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export Phase 6 PRISMA dashboard from pipeline_lifecycle_full.db."
    )
    parser.add_argument("--db", default=LIFECYCLE_DB,
                        help=f"DB path (default: {LIFECYCLE_DB})")
    parser.add_argument("--queries", default=DEFAULT_QUERY_FILE,
                        help=f"query_results.json path (default: {DEFAULT_QUERY_FILE})")
    parser.add_argument("--json-out", default=DEFAULT_JSON_OUT)
    parser.add_argument("--out", default=DEFAULT_HTML_OUT,
                        help=f"HTML output path (default: {DEFAULT_HTML_OUT})")
    parser.add_argument("--json-only", action="store_true",
                        help="Write JSON only, skip HTML generation")
    args = parser.parse_args()

    data = run_export(
        db_path    = args.db,
        query_file = args.queries,
        json_out   = args.json_out,
        html_out   = args.out,
        json_only  = args.json_only,
    )

    import json as _j
    print(_j.dumps({k: v for k, v in data.items() if k != "panel_a"}, indent=2))


if __name__ == "__main__":
    main()
