"""
triage_engine.py — Three-stage triage funnel for the article_references table.

STAGE 1 · Metadata-only screening
    Operates on every row with stage1_status = 'pending'.
    Zero network calls — pure metadata heuristics.
    Outputs: stage1_status ∈ {pass, fail}
    Records that fail are assigned final_decision = REJECT immediately.

STAGE 2 · Abstract enrichment + domain classifier
    Operates on rows with stage1_status = 'pass' AND stage2_status = 'pending'.
    Fetches abstracts via four-source fallback chain:
        semantic_scholar → crossref → pubmed → openalex
    Runs heuristic daylight-and-cognition domain classifier.
    Outputs: stage2_status ∈ {ACCEPT, EDGE_CASE, REJECT, MISSING_ABSTRACT, DUPLICATE}
    Also sets final_decision.

STAGE 3 · PDF acquisition (gated — NEVER before Stage 2 clears a record)
    Operates on rows with stage2_status ∈ {ACCEPT, EDGE_CASE}
                          AND stage3_status = 'pending'.
    Acquisition order:
        1. Unpaywall (open-access PDF URL)
        2. Direct DOI URL download attempt
        3. scidownl (Sci-Hub) — last resort
    Outputs: stage3_status ∈ {pdf_found, pdf_not_found}

The PRISMA dashboard is always rebuilt from get_prisma_counts() after each stage.

Usage
-----
    python triage_engine.py              # run all three stages
    python triage_engine.py --stage 1    # metadata screening only
    python triage_engine.py --stage 2    # abstract enrichment + classify
    python triage_engine.py --stage 3    # PDF acquisition
    python triage_engine.py --dry-run    # show pending counts, no changes
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path
from typing import Optional

import requests

from article_db import (
    DEFAULT_DB,
    get_pending_stage1,
    get_pending_stage2,
    get_pending_stage3,
    get_prisma_counts,
    init_db,
    set_final_decision,
    update_stage1,
    update_stage2,
    update_stage3,
)

# ── Decision constants ────────────────────────────────────────────────────────

DECISION_ACCEPT    = "ACCEPT"
DECISION_EDGE      = "EDGE_CASE"
DECISION_REJECT    = "REJECT"
DECISION_MISSING   = "MISSING_ABSTRACT"
DECISION_DUPLICATE = "DUPLICATE"

# ── API endpoints ─────────────────────────────────────────────────────────────

SEMANTIC_SCHOLAR_DOI    = "https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}"
SEMANTIC_SCHOLAR_SEARCH = "https://api.semanticscholar.org/graph/v1/paper/search"
CROSSREF_WORKS          = "https://api.crossref.org/works"
PUBMED_SEARCH           = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_FETCH            = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
OPENALEX_WORKS          = "https://api.openalex.org/works"
UNPAYWALL_BASE          = "https://api.unpaywall.org/v2"

HEADERS = {"User-Agent": "KA-TriageEngine/1.0 (mailto:student@ucsd.edu)"}


# ─────────────────────────────────────────────────────────────────────────────
# STAGE 1: Metadata-only screening
# ─────────────────────────────────────────────────────────────────────────────

def run_stage1(db_path: str = DEFAULT_DB) -> dict:
    """
    Lightweight metadata filter — no network calls.

    Fail conditions (any one is enough to reject):
      - title shorter than 5 characters
      - year outside [1990, 2030]
      - title is ALL-CAPS with length > 10 (junk signal from some scrapers)
    """
    records = get_pending_stage1(db_path)
    passed = failed = 0

    for rec in records:
        title = (rec.get("title") or "").strip()
        year  = rec.get("year")
        failures = []

        if len(title) < 5:
            failures.append("title_too_short")
        if year is not None:
            try:
                y = int(year)
                if y < 1990 or y > 2030:
                    failures.append("year_implausible")
            except (TypeError, ValueError):
                pass
        # ALL-CAPS title heuristic (length > 10 avoids catching short acronyms)
        if title and len(title) > 10 and title == title.upper():
            failures.append("all_caps_title")

        if failures:
            update_stage1(rec["paper_id"], "fail", db_path=db_path)
            set_final_decision(rec["paper_id"], DECISION_REJECT, db_path=db_path)
            failed += 1
        else:
            update_stage1(rec["paper_id"], "pass", db_path=db_path)
            passed += 1

    return {"stage1_passed": passed, "stage1_failed": failed}


# ─────────────────────────────────────────────────────────────────────────────
# STAGE 2: Abstract enrichment + heuristic classifier
# ─────────────────────────────────────────────────────────────────────────────

# ── Abstract fetchers ─────────────────────────────────────────────────────────

def _ss_doi(doi: str) -> str:
    try:
        resp = requests.get(
            SEMANTIC_SCHOLAR_DOI.format(doi=doi),
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
            SEMANTIC_SCHOLAR_SEARCH,
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


def _crossref_doi(doi: str) -> str:
    try:
        resp = requests.get(
            f"{CROSSREF_WORKS}/{doi}", timeout=15, headers=HEADERS
        )
        if resp.status_code == 200:
            return _strip_jats(resp.json().get("message", {}).get("abstract", ""))
    except Exception:
        pass
    return ""


def _crossref_title(title: str) -> str:
    try:
        resp = requests.get(
            CROSSREF_WORKS,
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
            PUBMED_SEARCH,
            params={"db": "pubmed", "term": title, "retmax": 1, "retmode": "json"},
            timeout=15,
        )
        ids = s.json().get("esearchresult", {}).get("idlist", [])
        if not ids:
            return ""
        f = requests.get(
            PUBMED_FETCH,
            params={"db": "pubmed", "id": ids[0], "retmode": "xml", "rettype": "abstract"},
            timeout=15,
        )
        m = re.search(r"<AbstractText[^>]*>(.*?)</AbstractText>", f.text, re.DOTALL)
        return m.group(1).strip() if m else ""
    except Exception:
        pass
    return ""


def _openalex_doi(doi: str) -> str:
    try:
        resp = requests.get(
            f"{OPENALEX_WORKS}/https://doi.org/{doi}",
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
            OPENALEX_WORKS,
            params={
                "search": title,
                "per-page": 1,
                "select": "abstract_inverted_index",
            },
            timeout=15, headers=HEADERS,
        )
        if resp.status_code == 200:
            results = resp.json().get("results", [])
            if results:
                return _decode_inverted(results[0].get("abstract_inverted_index"))
    except Exception:
        pass
    return ""


def _strip_jats(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


def _decode_inverted(inv: Optional[dict]) -> str:
    if not inv:
        return ""
    pairs = [(pos, w) for w, positions in inv.items() for pos in positions]
    pairs.sort()
    return " ".join(w for _, w in pairs)


def _fetch_abstract(doi: str, title: str) -> tuple[str, str]:
    """
    Try the four-source fallback chain.
    Returns (abstract_text, source_name).
    """
    # Source 1: Semantic Scholar
    if doi:
        ab = _ss_doi(doi)
        if ab:
            return ab, "semantic_scholar"
    ab = _ss_title(title)
    if ab:
        return ab, "semantic_scholar"
    time.sleep(0.3)

    # Source 2: Crossref
    if doi:
        ab = _crossref_doi(doi)
        if ab:
            return ab, "crossref"
    ab = _crossref_title(title)
    if ab:
        return ab, "crossref"
    time.sleep(0.3)

    # Source 3: PubMed
    ab = _pubmed_title(title)
    if ab:
        return ab, "pubmed"
    time.sleep(0.3)

    # Source 4: OpenAlex
    if doi:
        ab = _openalex_doi(doi)
        if ab:
            return ab, "openalex"
    ab = _openalex_title(title)
    if ab:
        return ab, "openalex"

    return "", "none"


# ── Domain relevance classifier ───────────────────────────────────────────────

_STRONG_SIGNALS = [
    "daylight", "daylighting", "natural light", "natural daylight",
    "circadian", "cognitive", "cognition", "attention", "working memory",
    "executive function", "n-back", "classroom", "workplace", "office worker",
    "biophilic", "thermal comfort", "indoor environment", "soundscape",
    "reverberation", "learning performance", "academic performance",
    "colour temperature", "correlated colour", "window-to-floor",
    "spatial daylight autonomy", "melanopsin", "alertness", "cognitive fatigue",
    "mental workload", "attentional restoration", "cognitive restoration",
]
_WEAK_SIGNALS = [
    "light", "environment", "building", "space", "design", "health",
    "indoor", "occupant", "school", "student", "office", "window",
]


def _heuristic_classify(title: str, abstract: str, snippet: str) -> str:
    """
    Two-threshold heuristic for the daylight-and-cognition domain.

    strong_hits >= 2  → ACCEPT
    strong_hits >= 1 OR weak_hits >= 3  → EDGE_CASE
    otherwise → REJECT
    """
    effective_abstract = abstract or snippet
    if len(effective_abstract.split()) < 20:
        return DECISION_MISSING

    combined = (title + " " + effective_abstract).lower()
    strong_hits = sum(1 for s in _STRONG_SIGNALS if s in combined)
    weak_hits   = sum(1 for w in _WEAK_SIGNALS   if w in combined)

    if strong_hits >= 2:
        return DECISION_ACCEPT
    if strong_hits >= 1 or weak_hits >= 3:
        return DECISION_EDGE
    return DECISION_REJECT


def run_stage2(db_path: str = DEFAULT_DB, *, delay: float = 0.5) -> dict:
    """
    Fetch abstracts and classify all records that passed Stage 1.

    Duplicate detection: if the same DOI appears more than once within
    this session, subsequent records are tagged DUPLICATE.
    """
    records   = get_pending_stage2(db_path)
    counts    = {
        DECISION_ACCEPT: 0, DECISION_EDGE: 0, DECISION_REJECT: 0,
        DECISION_MISSING: 0, DECISION_DUPLICATE: 0,
    }
    seen_dois: set[str] = set()

    for i, rec in enumerate(records, 1):
        title   = rec.get("title", "") or ""
        doi     = rec.get("doi", "") or ""
        snippet = rec.get("snippet", "") or ""
        print(f"  [stage2 {i:3d}/{len(records)}] {title[:60]}...")

        # Duplicate check
        if doi and doi in seen_dois:
            update_stage2(rec["paper_id"], status=DECISION_DUPLICATE, db_path=db_path)
            set_final_decision(rec["paper_id"], DECISION_DUPLICATE, db_path=db_path)
            counts[DECISION_DUPLICATE] += 1
            continue

        abstract, source = _fetch_abstract(doi, title)
        decision = _heuristic_classify(title, abstract, snippet)

        update_stage2(
            rec["paper_id"],
            status=decision,
            abstract=abstract or snippet,
            abstract_source=source if abstract else "snippet",
            doi=doi,
            db_path=db_path,
        )
        set_final_decision(rec["paper_id"], decision, db_path=db_path)
        counts[decision] = counts.get(decision, 0) + 1

        if doi:
            seen_dois.add(doi)

        if delay:
            time.sleep(delay)

    return counts


# ─────────────────────────────────────────────────────────────────────────────
# STAGE 3: PDF acquisition — gated, ACCEPT + EDGE_CASE only
# ─────────────────────────────────────────────────────────────────────────────

def _try_unpaywall(doi: str, email: str = "student@ucsd.edu") -> Optional[str]:
    """Return an open-access PDF URL from Unpaywall, or None."""
    try:
        resp = requests.get(
            f"{UNPAYWALL_BASE}/{doi}",
            params={"email": email},
            timeout=15,
        )
        if resp.status_code == 200:
            best = resp.json().get("best_oa_location") or {}
            return best.get("url_for_pdf")
    except Exception:
        pass
    return None


def _download_direct(url: str, out_path: str) -> bool:
    """Download a PDF from a direct URL; return True on success."""
    try:
        resp = requests.get(url, timeout=60, headers=HEADERS, stream=True)
        ctype = resp.headers.get("Content-Type", "")
        if resp.status_code == 200 and "pdf" in ctype.lower():
            with open(out_path, "wb") as fh:
                for chunk in resp.iter_content(8192):
                    fh.write(chunk)
            return Path(out_path).stat().st_size > 1000
    except Exception:
        pass
    return False


def run_stage3(
    db_path: str = DEFAULT_DB,
    *,
    pdf_dir: str = "pdfs",
    use_scidownl: bool = True,
    delay: float = 1.0,
) -> dict:
    """
    PDF acquisition for records cleared by Stage 2.

    THIS STAGE IS THE ONLY PLACE WHERE scidownl (Sci-Hub) IS INVOKED.
    harvest_layer.acquire_pdf_scidownl() is called here, never earlier.

    Acquisition order:
      1. Unpaywall open-access PDF link
      2. Direct DOI URL download
      3. scidownl (Sci-Hub) — last resort
    """
    records = get_pending_stage3(db_path)
    Path(pdf_dir).mkdir(parents=True, exist_ok=True)
    found = not_found = 0

    for i, rec in enumerate(records, 1):
        doi   = rec.get("doi", "") or ""
        title = (rec.get("title", "") or "")[:50]
        url   = rec.get("url", "") or ""
        print(f"  [stage3 {i:3d}/{len(records)}] {title}...")

        if not doi:
            update_stage3(rec["paper_id"], status="pdf_not_found", db_path=db_path)
            not_found += 1
            continue

        safe_doi = doi.replace("/", "_").replace(":", "_")
        out_path = str(Path(pdf_dir) / f"{safe_doi}.pdf")
        pdf_path: Optional[str] = None

        # Attempt 1 — Unpaywall (free open-access)
        oa_url = _try_unpaywall(doi)
        if oa_url and _download_direct(oa_url, out_path):
            pdf_path = out_path

        # Attempt 2 — Direct URL if it looks like a PDF endpoint
        if not pdf_path and url and ".pdf" in url.lower():
            if _download_direct(url, out_path):
                pdf_path = out_path

        # Attempt 3 — scidownl (Sci-Hub) last resort
        if not pdf_path and use_scidownl:
            from harvest_layer import acquire_pdf_scidownl  # import late — optional dep
            pdf_path = acquire_pdf_scidownl(doi, output_dir=pdf_dir)

        if pdf_path:
            update_stage3(rec["paper_id"], status="pdf_found", pdf_path=pdf_path, db_path=db_path)
            found += 1
        else:
            update_stage3(rec["paper_id"], status="pdf_not_found", db_path=db_path)
            not_found += 1

        if delay:
            time.sleep(delay)

    return {"pdf_found": found, "pdf_not_found": not_found}


# ─────────────────────────────────────────────────────────────────────────────
# Full pipeline runner
# ─────────────────────────────────────────────────────────────────────────────

def run_all_stages(
    db_path: str = DEFAULT_DB,
    *,
    pdf_dir: str = "pdfs",
    stage2_delay: float = 0.5,
    stage3_delay: float = 1.0,
) -> dict:
    """Run all three stages in order and return final PRISMA counts."""
    print("[triage] ── Stage 1: metadata screening ─────────────────────────")
    s1 = run_stage1(db_path)
    print(f"  Result: {s1}")

    print("[triage] ── Stage 2: abstract enrichment + classify ─────────────")
    s2 = run_stage2(db_path, delay=stage2_delay)
    print(f"  Result: {s2}")

    print("[triage] ── Stage 3: PDF acquisition (ACCEPT + EDGE_CASE only) ──")
    s3 = run_stage3(db_path, pdf_dir=pdf_dir, delay=stage3_delay)
    print(f"  Result: {s3}")

    counts = get_prisma_counts(db_path)
    print("\n[triage] PRISMA counts (live from article_references):")
    for k, v in counts.items():
        print(f"  {k:25s}: {v}")
    return counts


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Three-stage triage funnel for the article_references DB.\n"
            "Stage 1: metadata • Stage 2: abstract+classify • Stage 3: PDF (gated)"
        )
    )
    parser.add_argument(
        "--stage", type=int, choices=[1, 2, 3],
        help="Run a single stage (default: run all three)"
    )
    parser.add_argument("--db", default=DEFAULT_DB, help=f"DB path (default: {DEFAULT_DB})")
    parser.add_argument("--pdf-dir", default="pdfs", help="PDF output directory")
    parser.add_argument(
        "--no-scidownl", action="store_true",
        help="Disable Sci-Hub fallback in Stage 3"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print pending counts only — make no changes"
    )
    args = parser.parse_args()

    init_db(args.db)

    if args.dry_run:
        counts = get_prisma_counts(args.db)
        print("PRISMA counts (current):", counts)
        print("Pending stage 1:", len(get_pending_stage1(args.db)))
        print("Pending stage 2:", len(get_pending_stage2(args.db)))
        print("Pending stage 3:", len(get_pending_stage3(args.db)))
        return

    if args.stage == 1:
        print(run_stage1(args.db))
    elif args.stage == 2:
        print(run_stage2(args.db))
    elif args.stage == 3:
        print(run_stage3(args.db, pdf_dir=args.pdf_dir, use_scidownl=not args.no_scidownl))
    else:
        run_all_stages(args.db, pdf_dir=args.pdf_dir)


if __name__ == "__main__":
    main()
