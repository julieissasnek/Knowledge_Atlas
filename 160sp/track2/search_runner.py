"""
search_runner.py — Thin CLI wrapper around harvest_layer.scrape_serpapi().

This is the ad-hoc / spot-check entry point for the SerpAPI channel.
For bulk pipeline runs use harvest_layer.py or pipeline.py instead.

The SerpAPI implementation lives exclusively in harvest_layer.py so there
is exactly one place to update if the API changes.

Usage
-----
  # Ad-hoc query → stdout JSON
  python search_runner.py --query "daylight AND sustained attention"

  # Ad-hoc query → also insert into article_references DB
  python search_runner.py --query "daylight AND sustained attention" --db article_references.db

  # Run all 14 gap queries → stdout summary (use harvest_layer.py for DB writes)
  python search_runner.py --input query_results.json --num 5

  # Vary result count
  python search_runner.py --query "biophilic design cognition" --num 3
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Optional

# Import the canonical SerpAPI implementation from harvest_layer.
# search_runner.py must NOT re-implement SerpAPI logic — that belongs
# in harvest_layer.py only.
from harvest_layer import scrape_serpapi

DEFAULT_QUERY_FILE = "query_results.json"


# ── Public helper (used by spot_check.txt demonstrations) ─────────────────────

def search_google_scholar(
    query: str,
    *,
    num_results: int = 10,
) -> list[dict]:
    """
    Public thin wrapper kept for backward compatibility and spot-check scripts.

    Returns normalised candidate dicts with the correct 'snippet' field name.
    (Earlier versions of this file mis-labelled the field 'abstract' — SerpAPI
    returns 30–50 word snippets, not full abstracts.  Full abstracts are fetched
    in Triage Stage 2 via the abstract enrichment fallback chain.)
    """
    return scrape_serpapi(query, num=num_results)


# ── Batch runner (query_results.json → stdout summary) ────────────────────────

def run_from_query_file(
    query_file: str = DEFAULT_QUERY_FILE,
    *,
    results_per_gap: int = 5,
    delay: float = 1.0,
    db_path: Optional[str] = None,
) -> list[dict]:
    """
    Load gap queries from query_results.json and run SerpAPI for each.

    If db_path is provided, each candidate is also written to article_references
    via upsert_candidate().  This path is used when search_runner is acting as
    the harvest entry point rather than just a reporting tool.
    """
    path = Path(query_file)
    if not path.exists():
        print(f"[search_runner] {query_file} not found.", file=sys.stderr)
        return []

    if db_path:
        from article_db import init_db, upsert_candidate
        init_db(db_path)

    queries = json.loads(path.read_text(encoding="utf-8"))
    all_results: list[dict] = []

    for entry in queries:
        gap_id = entry.get("gap_id", "UNKNOWN")
        query_text = (
            entry.get("boolean_query") or entry.get("ai_citation_query") or ""
        ).strip()
        if not query_text:
            continue

        print(f"  [{gap_id}] {query_text[:80]}...")
        try:
            articles = scrape_serpapi(query_text, num=results_per_gap, gap_id=gap_id)
        except Exception as exc:
            print(f"  [{gap_id}] SerpAPI error: {exc}", file=sys.stderr)
            articles = []

        print(f"    → {len(articles)} results", end="")

        if db_path and articles:
            from article_db import upsert_candidate
            inserted = sum(1 for a in articles if upsert_candidate(a, db_path=db_path))
            print(f"  ({inserted} inserted into DB)", end="")
        print()

        all_results.extend(articles)
        if delay:
            time.sleep(delay)

    print(f"[search_runner] Total: {len(all_results)} SerpAPI results")
    return all_results


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Search Google Scholar via SerpAPI (delegates to harvest_layer.scrape_serpapi).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Notes:
  'snippet' (30-50 words) is what SerpAPI returns — NOT a full abstract.
  Full abstracts are fetched in Triage Stage 2 via the enrichment fallback chain.

  For bulk DB population use harvest_layer.py or pipeline.py directly.

Examples:
  python search_runner.py --query "daylight AND sustained attention" --num 3
  python search_runner.py --input query_results.json --num 5
  python search_runner.py --query "biophilic design" --db article_references.db
        """,
    )
    parser.add_argument("--query", "-q", help="Single ad-hoc search query (→ stdout JSON)")
    parser.add_argument(
        "--input", "-i", default=DEFAULT_QUERY_FILE,
        help=f"Gap query file (default: {DEFAULT_QUERY_FILE})"
    )
    parser.add_argument(
        "--num", "-n", type=int, default=5,
        help="Results per query (default: 5)"
    )
    parser.add_argument(
        "--db", default=None,
        help="Optional: also write results to this article_references DB path"
    )
    args = parser.parse_args()

    if args.query:
        results = scrape_serpapi(args.query, num=args.num)
        if args.db:
            from article_db import init_db, upsert_candidate
            init_db(args.db)
            inserted = sum(1 for r in results if upsert_candidate(r, db_path=args.db))
            print(f"[search_runner] Inserted {inserted}/{len(results)} into {args.db}",
                  file=sys.stderr)
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        results = run_from_query_file(
            args.input,
            results_per_gap=args.num,
            db_path=args.db,
        )
        # Print a summary table, not the full JSON blob
        print(f"\n{'GAP':14} {'TITLE':60} {'DOI':30} {'YEAR':6}")
        print("-" * 114)
        for r in results:
            title = (r.get("title") or "")[:58]
            doi   = (r.get("doi")   or "(none)")[:28]
            year  = str(r.get("year") or "")
            gap   = r.get("gap_id", "")
            print(f"{gap:14} {title:60} {doi:30} {year:6}")


if __name__ == "__main__":
    main()
