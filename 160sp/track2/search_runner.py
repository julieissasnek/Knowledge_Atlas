"""
search_runner.py — Search Google Scholar via SerpAPI and return articles
with DOI and abstract for each knowledge gap query.

Usage:
    python search_runner.py                        # reads query_results.json
    python search_runner.py --query "daylight cognition"
    python search_runner.py --help
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

import requests

SERP_API_KEY = os.environ.get(
    "SERP_API_KEY",
    "5263dbb7a48b42fb18fb48da509fd29322810760f0e03a1fae37daa1d30f71e0",
)
SERP_API_URL = "https://serpapi.com/search"
DEFAULT_QUERY_FILE = "query_results.json"
DEFAULT_OUT_FILE = "search_results.json"


def search_google_scholar(
    query: str,
    *,
    num_results: int = 10,
    api_key: str = SERP_API_KEY,
) -> list[dict]:
    """Run a single Google Scholar search and return normalised article records."""
    params = {
        "engine": "google_scholar",
        "q": query,
        "api_key": api_key,
        "num": num_results,
        "hl": "en",
    }
    resp = requests.get(SERP_API_URL, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    results: list[dict] = []
    for item in data.get("organic_results", []):
        pub_info = item.get("publication_info", {})
        summary = pub_info.get("summary", "")

        link = item.get("link", "") or ""
        doi = _extract_doi(link, item)

        results.append(
            {
                "title": item.get("title", ""),
                "abstract": item.get("snippet", ""),
                "doi": doi,
                "url": link,
                "authors": _parse_authors(pub_info),
                "year": _extract_year(summary),
                "cited_by": (item.get("inline_links") or {})
                .get("cited_by", {})
                .get("total"),
                "source": "google_scholar",
                "result_id": item.get("result_id", ""),
            }
        )
    return results


def _extract_doi(link: str, item: dict) -> str:
    if "doi.org/" in link:
        return link.split("doi.org/", 1)[1].split("?")[0].strip()
    resources = item.get("resources", [])
    for res in resources:
        href = (res.get("link") or "")
        if "doi.org/" in href:
            return href.split("doi.org/", 1)[1].split("?")[0].strip()
    return ""


def _parse_authors(pub_info: dict) -> list[str]:
    authors = pub_info.get("authors", [])
    if isinstance(authors, list):
        return [a.get("name", a) if isinstance(a, dict) else str(a) for a in authors]
    summary = pub_info.get("summary", "")
    if " - " in summary:
        return [a.strip() for a in summary.split(" - ")[0].split(",")]
    return []


def _extract_year(text: str) -> Optional[int]:
    import re
    m = re.search(r"\b(19|20)\d{2}\b", text)
    return int(m.group()) if m else None


def run_from_query_file(
    query_file: str = DEFAULT_QUERY_FILE,
    out_file: str = DEFAULT_OUT_FILE,
    *,
    results_per_gap: int = 5,
    delay: float = 1.0,
) -> list[dict]:
    """Load gap queries from query_results.json and run SerpAPI for each."""
    path = Path(query_file)
    if not path.exists():
        print(f"[search_runner] {query_file} not found — nothing to search.", file=sys.stderr)
        return []

    queries = json.loads(path.read_text(encoding="utf-8"))
    all_results: list[dict] = []

    for entry in queries:
        gap_id = entry.get("gap_id", "UNKNOWN")
        query_text = (
            entry.get("boolean_query")
            or entry.get("ai_citation_query")
            or ""
        ).strip()
        if not query_text:
            continue

        print(f"  [{gap_id}] Searching: {query_text[:80]}...")
        try:
            articles = search_google_scholar(query_text, num_results=results_per_gap)
        except Exception as exc:
            print(f"  [{gap_id}] Search failed: {exc}", file=sys.stderr)
            articles = []

        for art in articles:
            art["gap_id"] = gap_id
        all_results.extend(articles)

        if delay:
            time.sleep(delay)

    Path(out_file).write_text(
        json.dumps(all_results, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"[search_runner] Saved {len(all_results)} articles to {out_file}")
    return all_results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Search Google Scholar via SerpAPI for knowledge-gap queries.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python search_runner.py
  python search_runner.py --query "daylight AND cognition AND classroom"
  python search_runner.py --input gap_queries.json --output results.json
        """,
    )
    parser.add_argument("--query", "-q", help="Single ad-hoc search query")
    parser.add_argument(
        "--input", "-i", default=DEFAULT_QUERY_FILE,
        help=f"Gap query file (default: {DEFAULT_QUERY_FILE})"
    )
    parser.add_argument(
        "--output", "-o", default=DEFAULT_OUT_FILE,
        help=f"Output file (default: {DEFAULT_OUT_FILE})"
    )
    parser.add_argument(
        "--num", "-n", type=int, default=5,
        help="Results per query (default: 5)"
    )
    args = parser.parse_args()

    if args.query:
        results = search_google_scholar(args.query, num_results=args.num)
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        run_from_query_file(args.input, args.output, results_per_gap=args.num)


if __name__ == "__main__":
    main()
