"""
harvest_layer.py — Four-scraper harvest layer.

Every article candidate from every scraper is written into the
article_references SQLite table immediately upon retrieval.
The triage engine processes them later — in stages, never before.

Scrapers
--------
1. SerpAPI / google_scholar  — primary structured JSON source
2. scholarly                 — Google Scholar Python scraper (no API key)
3. paper-scraper             — PubMed + arXiv metadata via paperscraper package
4. scidownl                  — PDF ACQUISITION ONLY; called by triage_engine Stage 3,
                               NEVER during the harvest stage

Usage
-----
    python harvest_layer.py                              # run all scrapers on query_results.json
    python harvest_layer.py --query "daylight attention" --gap GAP-PNU-001
    python harvest_layer.py --scraper serpapi            # single scraper
    python harvest_layer.py --scraper serpapi,scholarly  # two scrapers
    python harvest_layer.py --dry-run                    # print without writing to DB
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

import requests

from article_db import DEFAULT_DB, init_db, upsert_candidate

# ── Phase 3: lifecycle DB bridge (optional — degrades if lifecycle_db absent) ──
try:
    import lifecycle_db as _lifecycle_db  # type: ignore
    _LIFECYCLE_AVAILABLE = True
except ImportError:
    _lifecycle_db = None  # type: ignore
    _LIFECYCLE_AVAILABLE = False

# ── Config ────────────────────────────────────────────────────────────────────

SERP_API_KEY = os.environ.get("SERP_API_KEY", "")
SERP_API_URL = "https://serpapi.com/search"
DEFAULT_QUERY_FILE = "query_results.json"
HEADERS = {"User-Agent": "KA-HarvestLayer/1.0 (mailto:student@ucsd.edu)"}


# ── ID generation ─────────────────────────────────────────────────────────────

def _make_paper_id(scraper: str, identifier: str) -> str:
    """Generate a stable paper_id from scraper name + any unique string."""
    h = hashlib.sha1(identifier.encode("utf-8", errors="replace")).hexdigest()[:10]
    return f"{scraper}-{h}"


# ── DOI / author / year helpers ───────────────────────────────────────────────

def _extract_doi(link: str, item: dict) -> str:
    if "doi.org/" in link:
        return link.split("doi.org/", 1)[1].split("?")[0].strip()
    for res in item.get("resources", []) or []:
        href = res.get("link") or ""
        if "doi.org/" in href:
            return href.split("doi.org/", 1)[1].split("?")[0].strip()
    return ""


def _parse_authors_serpapi(pub_info: dict) -> list[str]:
    authors = pub_info.get("authors", [])
    if isinstance(authors, list):
        return [a.get("name", a) if isinstance(a, dict) else str(a) for a in authors]
    summary = pub_info.get("summary", "")
    if " - " in summary:
        return [a.strip() for a in summary.split(" - ")[0].split(",")]
    return []


def _extract_year(text: str) -> Optional[int]:
    m = re.search(r"\b(19|20)\d{2}\b", str(text))
    return int(m.group()) if m else None


# ── Scraper 1: SerpAPI ────────────────────────────────────────────────────────

def scrape_serpapi(
    query: str,
    *,
    num: int = 5,
    gap_id: str = "",
    api_key: str = SERP_API_KEY,
) -> list[dict]:
    """
    Search Google Scholar via SerpAPI and return normalised candidate dicts.
    Returns an empty list on any error (graceful degradation).
    """
    try:
        params = {
            "engine": "google_scholar",
            "q": query,
            "api_key": api_key,
            "num": num,
            "hl": "en",
        }
        resp = requests.get(SERP_API_URL, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        print(f"  [serpapi] Error: {exc}", file=sys.stderr)
        return []

    candidates: list[dict] = []
    for item in data.get("organic_results", []):
        link = item.get("link", "") or ""
        doi = _extract_doi(link, item)
        pub_info = item.get("publication_info", {})
        summary = pub_info.get("summary", "")
        result_id = item.get("result_id", "") or link or item.get("title", "")
        candidates.append(
            {
                "paper_id": _make_paper_id("serpapi", result_id),
                "gap_id": gap_id,
                "scraper_source": "serpapi",
                "title": item.get("title", ""),
                "doi": doi,
                "url": link,
                "authors": _parse_authors_serpapi(pub_info),
                "year": _extract_year(summary),
                "venue": "",
                "cited_by": (item.get("inline_links") or {})
                    .get("cited_by", {})
                    .get("total"),
                "snippet": item.get("snippet", ""),
            }
        )
    return candidates


# ── Scraper 2: scholarly ──────────────────────────────────────────────────────

def scrape_scholarly(
    query: str,
    *,
    num: int = 5,
    gap_id: str = "",
) -> list[dict]:
    """
    Search Google Scholar via the `scholarly` Python package.

    Falls back gracefully if:
      - scholarly is not installed
      - Google rate-limits the request
    """
    try:
        from scholarly import scholarly as sch  # type: ignore
    except ImportError:
        print("  [scholarly] Not installed (pip install scholarly) — skipping.", file=sys.stderr)
        return []

    candidates: list[dict] = []
    try:
        search_gen = sch.search_pubs(query)
        for _ in range(num):
            try:
                pub = next(search_gen)
            except StopIteration:
                break
            except Exception as exc:
                print(f"  [scholarly] Iteration error: {exc}", file=sys.stderr)
                break

            bib = pub.get("bib", {})
            title = bib.get("title", "")
            raw_url = pub.get("pub_url", "") or ""
            doi = ""
            if "doi.org/" in raw_url:
                doi = raw_url.split("doi.org/", 1)[1].split("?")[0]

            scholars_url = pub.get("url_scholarbib", "") or raw_url or title
            candidates.append(
                {
                    "paper_id": _make_paper_id("scholarly", scholars_url),
                    "gap_id": gap_id,
                    "scraper_source": "scholarly",
                    "title": title,
                    "doi": doi,
                    "url": raw_url,
                    "authors": (
                        bib.get("author", [])
                        if isinstance(bib.get("author"), list)
                        else []
                    ),
                    "year": _extract_year(bib.get("pub_year", "")),
                    "venue": bib.get("venue", ""),
                    "cited_by": pub.get("num_citations"),
                    "snippet": bib.get("abstract", ""),
                }
            )
            time.sleep(0.5)  # polite scraping delay
    except Exception as exc:
        print(f"  [scholarly] Search failed: {exc}", file=sys.stderr)

    return candidates


# ── Scraper 3: paper-scraper (paperscraper) ───────────────────────────────────

def scrape_paper_scraper(
    query: str,
    *,
    num: int = 5,
    gap_id: str = "",
) -> list[dict]:
    """
    Search PubMed and arXiv via the `paperscraper` package.

    Falls back gracefully if paperscraper is not installed.
    """
    try:
        from paperscraper.pubmed import get_papers_from_pubmed  # type: ignore
        from paperscraper.arxiv import get_papers_from_arxiv    # type: ignore
    except ImportError:
        print(
            "  [paper-scraper] Not installed (pip install paperscraper) — skipping.",
            file=sys.stderr,
        )
        return []

    candidates: list[dict] = []
    keywords = [query]

    sources = [
        (get_papers_from_pubmed, "paper_scraper_pubmed"),
        (get_papers_from_arxiv, "paper_scraper_arxiv"),
    ]
    for fetch_fn, src_name in sources:
        try:
            results = fetch_fn(keywords, max_results=num)
            for paper in (results or [])[:num]:
                doi = paper.get("doi", "") or ""
                title = paper.get("title", "") or ""
                identifier = doi or title
                year_raw = paper.get("date", "") or ""
                year = _extract_year(year_raw[:4]) if year_raw else None
                candidates.append(
                    {
                        "paper_id": _make_paper_id(src_name, identifier),
                        "gap_id": gap_id,
                        "scraper_source": src_name,
                        "title": title,
                        "doi": doi,
                        "url": (
                            f"https://doi.org/{doi}" if doi else paper.get("url", "")
                        ),
                        "authors": paper.get("authors", []) or [],
                        "year": year,
                        "venue": paper.get("journal", "") or "",
                        "cited_by": None,
                        "snippet": paper.get("abstract", "") or "",
                    }
                )
        except Exception as exc:
            print(f"  [{src_name}] Failed: {exc}", file=sys.stderr)

    return candidates


# ── Scraper 4: scidownl — PDF ACQUISITION ONLY ───────────────────────────────
#
# acquire_pdf_scidownl() is intentionally placed here so that triage_engine.py
# can import it.  It is NEVER called by harvest_layer itself — only by
# triage_engine.run_stage3(), which enforces the Stage 2 gate.

def acquire_pdf_scidownl(doi: str, *, output_dir: str = "pdfs") -> Optional[str]:
    """
    Download a PDF via scidownl (Sci-Hub mirror).

    CONTRACT: This function MUST ONLY be called after a record has been
    assigned stage2_status ∈ {'ACCEPT', 'EDGE_CASE'} in article_references.
    The caller (triage_engine.run_stage3) enforces this invariant.

    Returns local PDF path on success, None on failure.
    """
    try:
        from scidownl import scihub_download  # type: ignore
    except ImportError:
        print("  [scidownl] Not installed (pip install scidownl) — skipping.", file=sys.stderr)
        return None

    Path(output_dir).mkdir(parents=True, exist_ok=True)
    safe = doi.replace("/", "_").replace(":", "_")
    out_path = str(Path(output_dir) / f"{safe}.pdf")
    try:
        scihub_download(doi, paper_type="doi", out=out_path)
        p = Path(out_path)
        if p.exists() and p.stat().st_size > 1000:
            return out_path
    except Exception as exc:
        print(f"  [scidownl] Failed for {doi}: {exc}", file=sys.stderr)
    return None


# ── Harvest coordinator ───────────────────────────────────────────────────────

def harvest_all_queries(
    query_file: str = DEFAULT_QUERY_FILE,
    *,
    db_path: str = DEFAULT_DB,
    results_per_gap: int = 5,
    delay: float = 1.0,
    scrapers: Optional[list[str]] = None,
    dry_run: bool = False,
    discovery_run_id: str = "",
    write_lifecycle: bool = True,
) -> int:
    """
    Load gap queries from query_results.json and run each enabled scraper.

    All candidates are written to article_references immediately.
    If write_lifecycle=True (default) and lifecycle_db is importable, candidates
    are also written to pipeline_lifecycle_full.db via lifecycle_db.write_candidates_to_lifecycle().

    Returns total number of new rows inserted into article_references.
    """
    if scrapers is None:
        scrapers = ["serpapi", "scholarly", "paper_scraper"]

    qpath = Path(query_file)
    if not qpath.exists():
        print(f"[harvest] {query_file} not found — nothing to harvest.", file=sys.stderr)
        return 0

    queries = json.loads(qpath.read_text(encoding="utf-8"))
    total_inserted = 0
    lifecycle_counts_total: dict[str, int] = {}

    for entry in queries:
        gap_id = entry.get("gap_id", "UNKNOWN")
        boolean_query  = (entry.get("boolean_query")    or "").strip()
        ai_query       = (entry.get("ai_citation_query") or "").strip()
        query_text     = boolean_query or ai_query
        if not query_text:
            continue

        print(f"  [{gap_id}] Harvesting: {query_text[:72]}...")
        all_candidates: list[dict] = []

        if "serpapi" in scrapers:
            results = scrape_serpapi(query_text, num=results_per_gap, gap_id=gap_id)
            print(f"    serpapi: {len(results)} results", end="")
            # Zero-result fallback: if boolean query yielded nothing and an
            # ai_citation_query is available, retry with that broader query.
            if len(results) == 0 and boolean_query and ai_query:
                print("  (0 results; retrying with ai_citation_query)", end="")
                results = scrape_serpapi(ai_query, num=results_per_gap, gap_id=gap_id)
                print(f"  {len(results)} results on retry", end="")
            print()
            all_candidates.extend(results)
            time.sleep(delay)

        if "scholarly" in scrapers:
            results = scrape_scholarly(query_text, num=results_per_gap, gap_id=gap_id)
            print(f"    scholarly: {len(results)} results")
            all_candidates.extend(results)
            time.sleep(delay)

        if "paper_scraper" in scrapers:
            results = scrape_paper_scraper(query_text, num=results_per_gap, gap_id=gap_id)
            print(f"    paper-scraper: {len(results)} results")
            all_candidates.extend(results)

        if dry_run:
            print(f"    [dry-run] would insert {len(all_candidates)} candidates")
        else:
            # ── Write to article_references (article_db) ──────────────────────
            for cand in all_candidates:
                if upsert_candidate(cand, db_path=db_path):
                    total_inserted += 1

            # ── Phase 3: mirror to pipeline_lifecycle_full.db ─────────────────
            if write_lifecycle and _LIFECYCLE_AVAILABLE and all_candidates:
                lc_counts = _lifecycle_db.write_candidates_to_lifecycle(
                    all_candidates,
                    discovery_run_id=discovery_run_id or gap_id,
                )
                for k, v in lc_counts.items():
                    lifecycle_counts_total[k] = lifecycle_counts_total.get(k, 0) + v

    if not dry_run:
        print(f"[harvest] {total_inserted} new candidates inserted into {db_path}")
        if write_lifecycle and _LIFECYCLE_AVAILABLE and lifecycle_counts_total:
            inserted_lc  = lifecycle_counts_total.get("inserted", 0)
            merged_lc    = lifecycle_counts_total.get("doi_merge", 0)
            skipped_lc   = lifecycle_counts_total.get("skipped", 0) + lifecycle_counts_total.get("error", 0)
            print(
                f"[lifecycle] pipeline_lifecycle_full.db — "
                f"inserted={inserted_lc}, doi_merge={merged_lc}, skipped/err={skipped_lc}"
            )
        elif write_lifecycle and not _LIFECYCLE_AVAILABLE:
            print("[lifecycle] lifecycle_db not importable — skipped (install lifecycle_db.py alongside harvest_layer.py)")
    return total_inserted


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Four-scraper harvest layer — writes all article candidates to "
            "article_references SQLite DB."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Scrapers available: serpapi, scholarly, paper_scraper
scidownl is NOT a harvest scraper — it is the Stage 3 PDF gate in triage_engine.py.

Examples:
  python harvest_layer.py
  python harvest_layer.py --scraper serpapi
  python harvest_layer.py --scraper serpapi,scholarly
  python harvest_layer.py --query "daylight attention office" --gap GAP-PNU-001
  python harvest_layer.py --dry-run
        """,
    )
    parser.add_argument("--query", "-q", help="Single ad-hoc search query")
    parser.add_argument("--gap", default="ADHOC", help="Gap ID for ad-hoc query")
    parser.add_argument(
        "--input", "-i", default=DEFAULT_QUERY_FILE,
        help=f"Gap query file (default: {DEFAULT_QUERY_FILE})"
    )
    parser.add_argument(
        "--num", "-n", type=int, default=5,
        help="Results per gap per scraper (default: 5)"
    )
    parser.add_argument(
        "--db", default=DEFAULT_DB,
        help=f"Database path (default: {DEFAULT_DB})"
    )
    parser.add_argument(
        "--scraper", default="serpapi,scholarly,paper_scraper",
        help="Comma-separated list of scrapers to run"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be inserted without writing to DB"
    )
    args = parser.parse_args()

    if not args.dry_run:
        init_db(args.db)

    scrapers = [s.strip() for s in args.scraper.split(",")]

    if args.query:
        print(f"[harvest] Ad-hoc query: {args.query}")
        candidates: list[dict] = []
        if "serpapi" in scrapers:
            candidates.extend(scrape_serpapi(args.query, num=args.num, gap_id=args.gap))
        if "scholarly" in scrapers:
            candidates.extend(scrape_scholarly(args.query, num=args.num, gap_id=args.gap))
        if "paper_scraper" in scrapers:
            candidates.extend(scrape_paper_scraper(args.query, num=args.num, gap_id=args.gap))

        if args.dry_run:
            print(json.dumps(candidates, indent=2, ensure_ascii=False))
        else:
            inserted = sum(
                1 for c in candidates if upsert_candidate(c, db_path=args.db)
            )
            print(f"Inserted {inserted}/{len(candidates)} candidates.")
    else:
        harvest_all_queries(
            args.input,
            db_path=args.db,
            results_per_gap=args.num,
            delay=1.0,
            scrapers=scrapers,
            dry_run=args.dry_run,
        )


if __name__ == "__main__":
    main()
