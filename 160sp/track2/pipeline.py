"""
pipeline.py — Full Track 2, Task 3 end-to-end pipeline orchestrator.

Runs the four-scraper harvest layer followed by the three-stage triage funnel
and then exports PRISMA counts and regenerates the dashboard HTML.

Stage order:
  1. gap_extractor   — generate PNU gaps and boolean/AI-citation queries
  2. harvest_layer   — run four scrapers, write all candidates to article_references
  3. triage_engine   — Stage 1 (metadata) → Stage 2 (abstract+classify) → Stage 3 (PDF)
  4. prisma_export   — export live counts to prisma_counts.json + ka_topic_proposer.html
  5. af_handoff_gen  — write af_handoff.json of ACCEPT records for Article Finder

The scidownl (Sci-Hub) scraper is ONLY invoked in Stage 3 of the triage engine,
after a record has been assigned stage2_status ∈ {ACCEPT, EDGE_CASE}.

Usage
-----
    python pipeline.py                 # full run (all stages)
    python pipeline.py --skip-harvest  # triage + export only (DB already populated)
    python pipeline.py --skip-stage3   # skip PDF acquisition
    python pipeline.py --dry-run       # print config, exit without running
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from article_db import DEFAULT_DB, get_all_records, get_prisma_counts, init_db
import gap_extractor
import harvest_layer
import triage_engine
import prisma_export


DEFAULT_HANDOFF_FILE = "af_handoff.json"


# ── Article Finder handoff export ─────────────────────────────────────────────

def export_af_handoff(
    db_path: str = DEFAULT_DB,
    out_file: str = DEFAULT_HANDOFF_FILE,
) -> list[dict]:
    """
    Write af_handoff.json containing all ACCEPT-tier records in the
    format expected by Article Finder's ingest/abstract_fetcher.py.
    """
    records = get_all_records(db_path)
    accept_records = [r for r in records if r.get("final_decision") == "ACCEPT"]

    handoff_records = []
    for r in accept_records:
        try:
            authors = json.loads(r.get("authors") or "[]")
        except (json.JSONDecodeError, TypeError):
            authors = []
        handoff_records.append(
            {
                "paper_id": r.get("paper_id", ""),
                "title": r.get("title", ""),
                "abstract": r.get("abstract", ""),
                "doi": r.get("doi", ""),
                "url": r.get("url", ""),
                "authors": authors,
                "year": r.get("year"),
                "source_gap": r.get("gap_id", ""),
                "triage_decision": "ACCEPT",
                "abstract_source": r.get("abstract_source", ""),
            }
        )

    handoff = {
        "handoff_version": "2.0",
        "produced_by": "Knowledge_Atlas/160sp/track2/pipeline.py",
        "produced_at": datetime.now(timezone.utc).isoformat(),
        "destination": "Article_Finder",
        "destination_intake": "ingest/abstract_fetcher.py",
        "topic": "daylight_and_cognition",
        "description": (
            "ACCEPT-tier records from Track 2 three-stage triage pipeline, "
            "ready for Article Finder full extraction."
        ),
        "record_count": len(handoff_records),
        "records": handoff_records,
    }

    Path(out_file).write_text(
        json.dumps(handoff, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"[pipeline] Wrote {len(handoff_records)} ACCEPT records to {out_file}")
    return handoff_records


# ── Full pipeline ─────────────────────────────────────────────────────────────

def run_pipeline(
    *,
    db_path: str = DEFAULT_DB,
    query_file: str = "query_results.json",
    results_per_gap: int = 5,
    scrapers: list[str] | None = None,
    skip_harvest: bool = False,
    skip_stage3: bool = False,
    pdf_dir: str = "pdfs",
) -> dict:
    """
    Run the complete Track 2 Task 3 pipeline.

    Returns final PRISMA counts dict.
    """
    if scrapers is None:
        scrapers = ["serpapi", "scholarly", "paper_scraper"]

    print("=" * 60)
    print("Knowledge Atlas — Track 2, Task 3 Pipeline")
    print("=" * 60)

    # Step 1: Ensure gap queries exist
    if not Path(query_file).exists():
        print("[pipeline] Generating gap queries...")
        gap_extractor.run_extraction()

    # Step 2: Initialise DB
    init_db(db_path)
    print(f"[pipeline] DB initialised: {db_path}")

    # Step 3: Harvest
    if not skip_harvest:
        print("\n[pipeline] ── HARVEST LAYER ────────────────────────────────")
        harvest_layer.harvest_all_queries(
            query_file,
            db_path=db_path,
            results_per_gap=results_per_gap,
            scrapers=scrapers,
        )
    else:
        print("[pipeline] Skipping harvest (--skip-harvest)")

    # Step 4: Triage Stages 1 + 2
    print("\n[pipeline] ── TRIAGE STAGE 1: metadata screening ────────────")
    s1 = triage_engine.run_stage1(db_path)
    print(f"  {s1}")

    print("\n[pipeline] ── TRIAGE STAGE 2: abstract enrichment + classify ─")
    s2 = triage_engine.run_stage2(db_path)
    print(f"  {s2}")

    # Step 5: Triage Stage 3 (PDF)
    if not skip_stage3:
        print("\n[pipeline] ── TRIAGE STAGE 3: PDF acquisition (gated) ───────")
        s3 = triage_engine.run_stage3(db_path, pdf_dir=pdf_dir)
        print(f"  {s3}")
    else:
        print("[pipeline] Skipping Stage 3 PDF acquisition (--skip-stage3)")

    # Step 6: PRISMA export
    print("\n[pipeline] ── PRISMA EXPORT ─────────────────────────────────")
    counts = prisma_export.regenerate_html(db_path)
    print(f"  PRISMA counts: {counts}")

    # Step 7: Article Finder handoff
    print("\n[pipeline] ── ARTICLE FINDER HANDOFF ───────────────────────")
    export_af_handoff(db_path)

    print("\n" + "=" * 60)
    print("Pipeline complete.")
    print("=" * 60)
    return counts


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Track 2, Task 3 end-to-end pipeline (harvest → triage → PRISMA → handoff).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python pipeline.py                        # full run
  python pipeline.py --skip-harvest         # triage + export on existing DB
  python pipeline.py --skip-stage3          # skip PDF acquisition
  python pipeline.py --scraper serpapi      # harvest with SerpAPI only
  python pipeline.py --dry-run             # print config and exit
        """,
    )
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument(
        "--scraper", default="serpapi,scholarly,paper_scraper",
        help="Comma-separated scrapers for harvest (default: all three)"
    )
    parser.add_argument(
        "--num", "-n", type=int, default=5,
        help="Results per gap per scraper"
    )
    parser.add_argument(
        "--skip-harvest", action="store_true",
        help="Skip harvest; run triage on existing DB rows"
    )
    parser.add_argument(
        "--skip-stage3", action="store_true",
        help="Skip Stage 3 PDF acquisition"
    )
    parser.add_argument(
        "--pdf-dir", default="pdfs",
        help="Directory for downloaded PDFs"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print configuration and exit without running"
    )
    args = parser.parse_args()

    scrapers = [s.strip() for s in args.scraper.split(",")]

    if args.dry_run:
        print("Pipeline configuration:")
        print(f"  DB path:      {args.db}")
        print(f"  Scrapers:     {scrapers}")
        print(f"  Results/gap:  {args.num}")
        print(f"  Skip harvest: {args.skip_harvest}")
        print(f"  Skip stage 3: {args.skip_stage3}")
        print(f"  PDF dir:      {args.pdf_dir}")
        sys.exit(0)

    run_pipeline(
        db_path=args.db,
        results_per_gap=args.num,
        scrapers=scrapers,
        skip_harvest=args.skip_harvest,
        skip_stage3=args.skip_stage3,
        pdf_dir=args.pdf_dir,
    )


if __name__ == "__main__":
    main()
