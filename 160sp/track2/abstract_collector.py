"""
abstract_collector.py — Enrich article records with full abstracts and
confirmed DOIs using a four-source fallback chain:

  1. Semantic Scholar  (primary — best coverage of computer science & life sciences)
  2. Crossref          (authoritative DOI registry with abstract metadata)
  3. PubMed / NCBI     (biomedical and cognitive science literature)
  4. OpenAlex          (open scholarly graph, broad coverage)

Input:  search_results.json  (produced by search_runner.py)
Output: triage_results.json  (each record gains doi, abstract, decision)

Usage:
    python abstract_collector.py
    python abstract_collector.py --input search_results.json --output triage_results.json
    python abstract_collector.py --help
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Optional

import requests

DEFAULT_IN_FILE = "search_results.json"
DEFAULT_OUT_FILE = "triage_results.json"

SEMANTIC_SCHOLAR_SEARCH = "https://api.semanticscholar.org/graph/v1/paper/search"
SEMANTIC_SCHOLAR_DOI = "https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}"
CROSSREF_WORKS = "https://api.crossref.org/works"
PUBMED_SEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_FETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
OPENALEX_WORKS = "https://api.openalex.org/works"

DECISION_ACCEPT = "ACCEPT"
DECISION_EDGE_CASE = "EDGE_CASE"
DECISION_REJECT = "REJECT"
DECISION_MISSING_ABSTRACT = "MISSING_ABSTRACT"
DECISION_DUPLICATE = "DUPLICATE"


# ── Source 1: Semantic Scholar ──────────────────────────────────────────────

def _semantic_scholar_by_doi(doi: str) -> Optional[dict]:
    try:
        url = SEMANTIC_SCHOLAR_DOI.format(doi=doi)
        resp = requests.get(
            url,
            params={"fields": "title,abstract,externalIds,year,authors"},
            timeout=15,
            headers={"User-Agent": "KA-AbstractCollector/1.0"},
        )
        if resp.status_code == 200:
            data = resp.json()
            return {
                "doi": doi,
                "abstract": data.get("abstract") or "",
                "title": data.get("title") or "",
                "year": data.get("year"),
                "source": "semantic_scholar",
            }
    except Exception:
        pass
    return None


def _semantic_scholar_by_title(title: str) -> Optional[dict]:
    try:
        resp = requests.get(
            SEMANTIC_SCHOLAR_SEARCH,
            params={
                "query": title,
                "fields": "title,abstract,externalIds,year",
                "limit": 1,
            },
            timeout=15,
            headers={"User-Agent": "KA-AbstractCollector/1.0"},
        )
        if resp.status_code == 200:
            items = resp.json().get("data", [])
            if items:
                item = items[0]
                doi = (item.get("externalIds") or {}).get("DOI", "")
                return {
                    "doi": doi,
                    "abstract": item.get("abstract") or "",
                    "title": item.get("title") or "",
                    "year": item.get("year"),
                    "source": "semantic_scholar",
                }
    except Exception:
        pass
    return None


# ── Source 2: Crossref ───────────────────────────────────────────────────────

def _crossref_by_doi(doi: str) -> Optional[dict]:
    try:
        resp = requests.get(
            f"{CROSSREF_WORKS}/{doi}",
            timeout=15,
            headers={"User-Agent": "KA-AbstractCollector/1.0 (mailto:student@ucsd.edu)"},
        )
        if resp.status_code == 200:
            item = resp.json().get("message", {})
            abstract = _strip_jats(item.get("abstract", ""))
            return {
                "doi": doi,
                "abstract": abstract,
                "title": " ".join(item.get("title", [])),
                "year": (item.get("issued") or {}).get("date-parts", [[None]])[0][0],
                "source": "crossref",
            }
    except Exception:
        pass
    return None


def _crossref_by_title(title: str) -> Optional[dict]:
    try:
        resp = requests.get(
            CROSSREF_WORKS,
            params={
                "query.title": title,
                "rows": 1,
                "select": "DOI,title,abstract,issued",
            },
            timeout=15,
            headers={"User-Agent": "KA-AbstractCollector/1.0 (mailto:student@ucsd.edu)"},
        )
        if resp.status_code == 200:
            items = resp.json().get("message", {}).get("items", [])
            if items:
                item = items[0]
                abstract = _strip_jats(item.get("abstract", ""))
                return {
                    "doi": item.get("DOI", ""),
                    "abstract": abstract,
                    "title": " ".join(item.get("title", [])),
                    "year": (item.get("issued") or {}).get("date-parts", [[None]])[0][0],
                    "source": "crossref",
                }
    except Exception:
        pass
    return None


def _strip_jats(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


# ── Source 3: PubMed ─────────────────────────────────────────────────────────

def _pubmed_by_title(title: str) -> Optional[dict]:
    try:
        search_resp = requests.get(
            PUBMED_SEARCH,
            params={
                "db": "pubmed",
                "term": title,
                "retmax": 1,
                "retmode": "json",
            },
            timeout=15,
        )
        if search_resp.status_code != 200:
            return None
        ids = search_resp.json().get("esearchresult", {}).get("idlist", [])
        if not ids:
            return None

        fetch_resp = requests.get(
            PUBMED_FETCH,
            params={
                "db": "pubmed",
                "id": ids[0],
                "retmode": "xml",
                "rettype": "abstract",
            },
            timeout=15,
        )
        if fetch_resp.status_code != 200:
            return None

        xml = fetch_resp.text
        abstract_match = re.search(r"<AbstractText[^>]*>(.*?)</AbstractText>", xml, re.DOTALL)
        abstract = abstract_match.group(1).strip() if abstract_match else ""
        doi_match = re.search(r'ArticleId IdType="doi">(.*?)</ArticleId>', xml)
        doi = doi_match.group(1).strip() if doi_match else ""
        title_match = re.search(r"<ArticleTitle>(.*?)</ArticleTitle>", xml, re.DOTALL)
        found_title = re.sub(r"<[^>]+>", "", title_match.group(1)).strip() if title_match else ""
        year_match = re.search(r"<PubDate>.*?<Year>(\d{4})</Year>", xml, re.DOTALL)
        year = int(year_match.group(1)) if year_match else None

        return {
            "doi": doi,
            "abstract": abstract,
            "title": found_title,
            "year": year,
            "source": "pubmed",
        }
    except Exception:
        pass
    return None


# ── Source 4: OpenAlex ───────────────────────────────────────────────────────

def _openalex_by_doi(doi: str) -> Optional[dict]:
    try:
        resp = requests.get(
            f"{OPENALEX_WORKS}/https://doi.org/{doi}",
            params={"select": "title,abstract_inverted_index,doi,publication_year"},
            timeout=15,
            headers={"User-Agent": "KA-AbstractCollector/1.0"},
        )
        if resp.status_code == 200:
            data = resp.json()
            abstract = _decode_inverted_index(data.get("abstract_inverted_index"))
            return {
                "doi": doi,
                "abstract": abstract,
                "title": data.get("title") or "",
                "year": data.get("publication_year"),
                "source": "openalex",
            }
    except Exception:
        pass
    return None


def _openalex_by_title(title: str) -> Optional[dict]:
    try:
        resp = requests.get(
            OPENALEX_WORKS,
            params={
                "search": title,
                "per-page": 1,
                "select": "title,abstract_inverted_index,doi,publication_year",
            },
            timeout=15,
            headers={"User-Agent": "KA-AbstractCollector/1.0"},
        )
        if resp.status_code == 200:
            results = resp.json().get("results", [])
            if results:
                data = results[0]
                abstract = _decode_inverted_index(data.get("abstract_inverted_index"))
                doi_raw = data.get("doi", "")
                doi = doi_raw.replace("https://doi.org/", "").strip() if doi_raw else ""
                return {
                    "doi": doi,
                    "abstract": abstract,
                    "title": data.get("title") or "",
                    "year": data.get("publication_year"),
                    "source": "openalex",
                }
    except Exception:
        pass
    return None


def _decode_inverted_index(inverted: Optional[dict]) -> str:
    if not inverted:
        return ""
    word_positions: list[tuple[int, str]] = []
    for word, positions in inverted.items():
        for pos in positions:
            word_positions.append((pos, word))
    word_positions.sort()
    return " ".join(w for _, w in word_positions)


# ── Fallback chain ────────────────────────────────────────────────────────────

def enrich_article(article: dict, *, delay: float = 0.3) -> dict:
    """Try each source in order until we get a non-empty abstract."""
    title = article.get("title", "")
    doi = article.get("doi", "")

    result = None

    # Source 1: Semantic Scholar
    if doi:
        result = _semantic_scholar_by_doi(doi)
    if not result or not result.get("abstract"):
        result = _semantic_scholar_by_title(title)
    if result and result.get("abstract"):
        return {**article, **result, "enriched": True}
    time.sleep(delay)

    # Source 2: Crossref
    if doi:
        result = _crossref_by_doi(doi)
    if not result or not result.get("abstract"):
        result = _crossref_by_title(title)
    if result and result.get("abstract"):
        return {**article, **result, "enriched": True}
    time.sleep(delay)

    # Source 3: PubMed
    result = _pubmed_by_title(title)
    if result and result.get("abstract"):
        return {**article, **result, "enriched": True}
    time.sleep(delay)

    # Source 4: OpenAlex
    if doi:
        result = _openalex_by_doi(doi)
    if not result or not result.get("abstract"):
        result = _openalex_by_title(title)
    if result and result.get("abstract"):
        return {**article, **result, "enriched": True}

    # Keep the Scholar snippet as best available abstract
    return {
        **article,
        "enriched": False,
        "abstract": article.get("abstract", ""),
        "source_enrichment": "none",
    }


# ── Triage decision ───────────────────────────────────────────────────────────

def _triage_decision(record: dict, seen_dois: set[str]) -> str:
    doi = record.get("doi", "")
    abstract = (record.get("abstract") or "").strip()

    if doi and doi in seen_dois:
        return DECISION_DUPLICATE
    if not abstract:
        return DECISION_MISSING_ABSTRACT

    word_count = len(abstract.split())
    if word_count < 20:
        return DECISION_MISSING_ABSTRACT

    # Heuristic topic relevance for daylight-and-cognition domain
    text = (record.get("title", "") + " " + abstract).lower()
    strong_signals = [
        "daylight", "daylighting", "natural light", "circadian", "cognitive",
        "cognition", "attention", "working memory", "classroom", "office",
        "workplace", "biophilic", "thermal comfort", "indoor environment",
        "soundscape", "noise", "learning performance", "academic performance",
    ]
    weak_signals = ["light", "environment", "building", "space", "design", "health"]

    strong_hits = sum(1 for s in strong_signals if s in text)
    weak_hits = sum(1 for w in weak_signals if w in text)

    if strong_hits >= 2:
        return DECISION_ACCEPT
    if strong_hits >= 1 or weak_hits >= 3:
        return DECISION_EDGE_CASE
    return DECISION_REJECT


def collect_abstracts(
    in_file: str = DEFAULT_IN_FILE,
    out_file: str = DEFAULT_OUT_FILE,
    *,
    delay: float = 0.5,
) -> list[dict]:
    in_path = Path(in_file)
    if not in_path.exists():
        print(f"[abstract_collector] {in_file} not found.", file=sys.stderr)
        return []

    articles = json.loads(in_path.read_text(encoding="utf-8"))
    triage: list[dict] = []
    seen_dois: set[str] = set()

    for i, art in enumerate(articles, 1):
        title = art.get("title", "")[:60]
        print(f"  [{i}/{len(articles)}] Enriching: {title}...")
        enriched = enrich_article(art, delay=delay)
        decision = _triage_decision(enriched, seen_dois)
        enriched["decision"] = decision

        doi = enriched.get("doi", "")
        if doi and decision != DECISION_DUPLICATE:
            seen_dois.add(doi)

        triage.append(enriched)

    Path(out_file).write_text(
        json.dumps(triage, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    counts = {d: sum(1 for r in triage if r.get("decision") == d) for d in [
        DECISION_ACCEPT, DECISION_EDGE_CASE, DECISION_REJECT,
        DECISION_MISSING_ABSTRACT, DECISION_DUPLICATE,
    ]}
    print(f"[abstract_collector] {len(triage)} records → {out_file}")
    print(f"  Decisions: {counts}")
    return triage


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Enrich search results with full abstracts and DOIs "
            "via semantic_scholar → crossref → pubmed → openalex fallback chain."
        )
    )
    parser.add_argument(
        "--input", "-i", default=DEFAULT_IN_FILE,
        help=f"Input JSON from search_runner (default: {DEFAULT_IN_FILE})"
    )
    parser.add_argument(
        "--output", "-o", default=DEFAULT_OUT_FILE,
        help=f"Output triage JSON (default: {DEFAULT_OUT_FILE})"
    )
    parser.add_argument(
        "--delay", "-d", type=float, default=0.5,
        help="Seconds between API calls (default: 0.5)"
    )
    args = parser.parse_args()
    collect_abstracts(args.input, args.output, delay=args.delay)


if __name__ == "__main__":
    main()
