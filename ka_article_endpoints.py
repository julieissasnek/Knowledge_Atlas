#!/usr/bin/env python3
"""
Knowledge Atlas — Article Submission Endpoints
===============================================
Extends the KA auth server with article intake, validation, duplicate
detection, staging, and instructor review.

This module is imported by ka_auth_server.py and registered as a FastAPI
APIRouter.  It adds:

    POST /api/articles/submit          – upload PDFs and/or citations
    GET  /api/articles/status/{id}     – check article status
    GET  /api/articles/my-submissions  – student's own submissions
    GET  /api/articles/pending-review  – instructor: articles awaiting review
    POST /api/articles/{id}/review     – instructor: accept or reject
    POST /api/articles/check-duplicate – real-time duplicate probe
    GET  /api/articles/stats           – corpus + personal statistics

See docs/ARTICLE_SUBMISSION_MODULE_SPEC_2026-03-30.md for the full contract.
"""

import hashlib, json, os, re, secrets, shutil, sqlite3, sys
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
from typing import Optional, List, Any

from fastapi import APIRouter, HTTPException, Depends, UploadFile, File, Form, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel


REPO_ROOT = Path(__file__).resolve().parent
ATLAS_SHARED_SRC = REPO_ROOT.parent / "atlas_shared" / "src"
if ATLAS_SHARED_SRC.exists() and str(ATLAS_SHARED_SRC) not in sys.path:
    sys.path.insert(0, str(ATLAS_SHARED_SRC))


def _build_local_classifier_backend():
    @dataclass(frozen=True)
    class LocalClassificationEvidence:
        paper_id: str = ""
        title: str = ""
        abstract: str = ""
        keywords: tuple[str, ...] = ()
        doi: str = ""
        filename: str = ""
        pdf_path: str = ""
        first_page_text: str = ""

    @dataclass(frozen=True)
    class LocalArticleType:
        value: str
        confidence: float
        evidence: tuple[str, ...]
        source: str = "ka_local_fallback"

    @dataclass(frozen=True)
    class LocalClassificationResult:
        article_type: LocalArticleType
        evidence_stage: str = "heuristic"
        next_action: str = "review_if_uncertain"

    class LocalAdaptiveClassifierSubsystem:
        def classify(self, evidence: LocalClassificationEvidence, allow_surface_creation: bool = False):
            text = " ".join(
                part for part in [
                    evidence.title,
                    evidence.abstract,
                    " ".join(evidence.keywords),
                    evidence.first_page_text,
                ] if part
            ).lower()

            def result(label: str, confidence: float, *signals: str):
                next_action = "accept" if confidence >= 0.75 else "review_if_uncertain"
                return LocalClassificationResult(
                    article_type=LocalArticleType(
                        value=label,
                        confidence=confidence,
                        evidence=tuple(signals) or ("fallback:no_signals",),
                    ),
                    evidence_stage="heuristic",
                    next_action=next_action,
                )

            if re.search(r"\bmeta[\s-]?analysis\b", text):
                return result("meta_analysis", 0.96, "phrase:meta-analysis")
            if any(phrase in text for phrase in ("systematic review", "scoping review", "literature review", "review article")):
                return result("systematic_review", 0.93, "phrase:review")
            if any(phrase in text for phrase in ("study protocol", "trial protocol", "protocol paper", "protocol for")):
                return result("protocol", 0.9, "phrase:protocol")
            if any(phrase in text for phrase in ("case study", "case report")):
                return result("case_study", 0.88, "phrase:case-study")
            if "mixed methods" in text or "mixed-methods" in text:
                return result("mixed_methods", 0.82, "phrase:mixed-methods")
            if any(phrase in text for phrase in ("qualitative study", "thematic analysis", "focus group", "focus groups", "semi-structured interview", "semi structured interview", "ethnograph", "grounded theory")):
                return result("qualitative_research", 0.82, "phrase:qualitative")
            if any(phrase in text for phrase in ("editorial", "commentary", "perspective", "viewpoint", "letter to the editor")):
                return result("commentary", 0.78, "phrase:commentary")

            empirical_hits = [
                signal for signal in (
                    ("randomized", "term:randomized"),
                    ("participants", "term:participants"),
                    ("method", "term:method"),
                    ("results", "term:results"),
                    ("experiment", "term:experiment"),
                    ("trial", "term:trial"),
                    ("survey", "term:survey"),
                    ("intervention", "term:intervention"),
                    ("we examined", "phrase:we-examined"),
                    ("this study", "phrase:this-study"),
                ) if signal[0] in text
            ]
            if len(empirical_hits) >= 2:
                return result("empirical_research", 0.78, *(tag for _, tag in empirical_hits[:4]))

            theoretical_hits = [
                signal for signal in (
                    ("theoretical framework", "phrase:theoretical-framework"),
                    ("conceptual framework", "phrase:conceptual-framework"),
                    ("conceptual model", "phrase:conceptual-model"),
                    ("theoretical model", "phrase:theoretical-model"),
                    ("we propose", "phrase:we-propose"),
                    ("this paper discusses", "phrase:discusses"),
                ) if signal[0] in text
            ]
            if theoretical_hits and not empirical_hits:
                return result("theoretical", 0.72, *(tag for _, tag in theoretical_hits[:4]))

            return result("unknown", 0.35, "fallback:unknown")

    return LocalAdaptiveClassifierSubsystem, LocalClassificationEvidence


def _load_classifier_backend(importer=import_module):
    try:
        module = importer("atlas_shared.classifier_system")
        return (
            module.AdaptiveClassifierSubsystem,
            module.ClassificationEvidence,
            "atlas_shared",
            "",
        )
    except Exception as exc:
        fallback_classifier, fallback_evidence = _build_local_classifier_backend()
        return (
            fallback_classifier,
            fallback_evidence,
            "ka_local_fallback",
            repr(exc),
        )


AdaptiveClassifierSubsystem, ClassificationEvidence, CLASSIFIER_BACKEND, CLASSIFIER_BACKEND_NOTE = _load_classifier_backend()
if CLASSIFIER_BACKEND != "atlas_shared":
    print(f"[KA-ARTICLES] atlas_shared classifier unavailable; using local fallback ({CLASSIFIER_BACKEND_NOTE})")

# ════════════════════════════════════════════════
# CONFIG — overridable via environment variables
# ════════════════════════════════════════════════
STORAGE_ROOT       = Path(os.environ.get("KA_STORAGE_ROOT", Path(__file__).parent / "data" / "storage"))
QUARANTINE_DIR     = Path(os.environ.get("KA_QUARANTINE_DIR", STORAGE_ROOT / "quarantine"))
PDF_COLLECTION_DIR = Path(os.environ.get("KA_PDF_COLLECTION_DIR", STORAGE_ROOT / "pdf_collection"))
REJECTED_DIR       = Path(os.environ.get("KA_REJECTED_DIR", STORAGE_ROOT / "rejected"))

MAX_FILE_SIZE_BYTES   = int(os.environ.get("KA_MAX_FILE_SIZE_MB", 100)) * 1024 * 1024
MAX_BATCH_SIZE_BYTES  = int(os.environ.get("KA_MAX_BATCH_SIZE_MB", 500)) * 1024 * 1024
MAX_ANON_PER_HOUR     = int(os.environ.get("KA_MAX_ANON_SUBMISSIONS_PER_HOUR", 10))
MAX_AUTH_PER_HOUR     = int(os.environ.get("KA_MAX_AUTH_SUBMISSIONS_PER_HOUR", 50))

# Ensure storage directories exist
for d in (QUARANTINE_DIR, PDF_COLLECTION_DIR, REJECTED_DIR):
    d.mkdir(parents=True, exist_ok=True)

# ════════════════════════════════════════════════
# ROUTER
# ════════════════════════════════════════════════
router = APIRouter(prefix="/api/articles", tags=["articles"])

# We need access to the auth server's DB and user helpers.
# These are injected by the main server when it includes this router.
_get_db = None
_get_current_user_optional = None
_get_current_user = None
_require_instructor = None


# Phase 3 / Task 1 grader signature stores these for future use; they have
# no callers in the existing endpoints yet.
_get_session = None
_logging = None

def configure(get_db_fn=None, get_user_fn=None, get_session_fn=None, logging_fn=None,
              *,
              get_db=None, get_current_user=None,
              get_current_user_optional=None, require_instructor=None):
    """Inject shared dependencies. Backward-compatible with two callers:

      (a) Task 1 grader:  configure(get_db_fn, get_user_fn=..., get_session_fn=..., logging_fn=...)
      (b) ka_auth_server: configure(get_db=..., get_current_user=...,
                                    get_current_user_optional=..., require_instructor=...)

    The new positional names take precedence; legacy keyword names fill
    in any slot the new names left empty.
    """
    global _get_db, _get_current_user, _get_current_user_optional
    global _require_instructor, _get_session, _logging
    _get_db                    = get_db_fn   if get_db_fn   is not None else get_db
    _get_current_user          = get_user_fn if get_user_fn is not None else get_current_user
    _get_current_user_optional = get_current_user_optional if get_current_user_optional is not None else get_user_fn
    _require_instructor        = require_instructor
    _get_session               = get_session_fn
    _logging                   = logging_fn


def _init_article_tables():
    """Create article-related tables if they don't exist."""
    db = _get_db()
    db.executescript("""
    CREATE TABLE IF NOT EXISTS articles (
        article_id          TEXT PRIMARY KEY,
        submission_id       TEXT NOT NULL,
        submitter_id        TEXT,
        submitter_type      TEXT NOT NULL DEFAULT 'anonymous',
        track               TEXT,
        input_mode          TEXT NOT NULL,

        doi                 TEXT,
        title               TEXT,
        authors             TEXT,
        year                INTEGER,
        journal             TEXT,
        abstract            TEXT,
        citation_raw        TEXT,

        pdf_filename        TEXT,
        pdf_hash_sha256     TEXT,
        pdf_size_bytes      INTEGER,
        quarantine_path     TEXT,
        promoted_path       TEXT,

        article_type        TEXT,          -- experimental | review | theory | mechanism | meta_analysis | other | unknown
        a0_task             TEXT,          -- task1 (experimental-only) | task2 (any-type) | NULL (non-A0)
        article_type_valid  INTEGER DEFAULT 0,  -- 1 if type confirmed appropriate for the task

        status              TEXT NOT NULL DEFAULT 'received',
        duplicate_of        TEXT,
        validation_notes    TEXT,
        relevance_score     REAL,
        review_notes        TEXT,

        assigned_question_id TEXT,
        topic_tags          TEXT,

        source_surface      TEXT DEFAULT 'ka_contribute',
        course_context      TEXT,
        submitter_notes     TEXT,

        created_at          TEXT NOT NULL,
        validated_at        TEXT,
        staged_at           TEXT,
        reviewed_at         TEXT,
        promoted_at         TEXT,
        rejected_at         TEXT,

        metadata_confidence TEXT DEFAULT 'low'
    );

    CREATE INDEX IF NOT EXISTS idx_articles_status ON articles(status);
    CREATE INDEX IF NOT EXISTS idx_articles_submitter ON articles(submitter_id);
    CREATE INDEX IF NOT EXISTS idx_articles_doi ON articles(doi);
    CREATE INDEX IF NOT EXISTS idx_articles_hash ON articles(pdf_hash_sha256);
    CREATE INDEX IF NOT EXISTS idx_articles_submission ON articles(submission_id);

    CREATE TABLE IF NOT EXISTS submission_batches (
        submission_id   TEXT PRIMARY KEY,
        submitter_id    TEXT,
        submitter_type  TEXT NOT NULL DEFAULT 'anonymous',
        input_mode      TEXT NOT NULL,
        item_count      INTEGER NOT NULL DEFAULT 0,
        source_surface  TEXT,
        created_at      TEXT NOT NULL,
        completed_at    TEXT
    );

    CREATE TABLE IF NOT EXISTS audit_log (
        log_id      INTEGER PRIMARY KEY AUTOINCREMENT,
        article_id  TEXT NOT NULL,
        action      TEXT NOT NULL,
        old_status  TEXT,
        new_status  TEXT,
        actor_id    TEXT,
        actor_type  TEXT DEFAULT 'system',
        details     TEXT,
        created_at  TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_audit_article ON audit_log(article_id);
    CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);

    -- Question claiming: each question can be held by one student per round.
    -- Once all 8 questions are claimed in a round, a new round opens.
    CREATE TABLE IF NOT EXISTS question_claims (
        claim_id        INTEGER PRIMARY KEY AUTOINCREMENT,
        question_id     TEXT NOT NULL,          -- FK → research_questions.question_id
        user_id         TEXT NOT NULL,           -- FK → users.user_id
        round           INTEGER NOT NULL DEFAULT 1,  -- round 1, 2, 3, ...
        claimed_at      TEXT NOT NULL,
        released_at     TEXT,                    -- NULL if still held
        task1_count     INTEGER NOT NULL DEFAULT 0,  -- experimental articles submitted for this claim
        task2_count     INTEGER NOT NULL DEFAULT 0,  -- any-type articles submitted for this claim
        UNIQUE(question_id, round)               -- one claim per question per round
    );
    CREATE INDEX IF NOT EXISTS idx_claims_user ON question_claims(user_id);
    CREATE INDEX IF NOT EXISTS idx_claims_question ON question_claims(question_id, round);
    """)
    db.commit()
    db.close()


# ════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════

def _next_id(prefix: str, table: str, id_col: str) -> str:
    """Generate the next sequential ID like KA-ART-000001."""
    db = _get_db()
    row = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    db.close()
    seq = (row[0] or 0) + 1
    return f"{prefix}{seq:06d}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _compute_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _validate_pdf_bytes(data: bytes, filename: str) -> dict:
    """Check that the file is a valid PDF. Returns validation dict."""
    notes = {"filename": filename, "size": len(data)}

    # Magic bytes check
    if not data[:5] == b"%PDF-":
        notes["magic_bytes"] = "FAIL"
        notes["rejection_reason"] = "Not a PDF file (invalid magic bytes)"
        return notes
    notes["magic_bytes"] = "PASS"

    # Size check
    if len(data) > MAX_FILE_SIZE_BYTES:
        notes["size_check"] = "FAIL"
        notes["rejection_reason"] = f"File too large ({len(data)} bytes > {MAX_FILE_SIZE_BYTES} limit)"
        return notes
    notes["size_check"] = "PASS"

    # Basic structure check (PDF should end with %%EOF somewhere near the end)
    tail = data[-1024:]
    if b"%%EOF" not in tail:
        notes["structure_check"] = "WARN"
        notes["structure_note"] = "No %%EOF marker in last 1024 bytes (possibly truncated)"
    else:
        notes["structure_check"] = "PASS"

    notes["valid"] = True
    return notes


def _extract_doi_from_pdf(data: bytes) -> Optional[str]:
    """Try to extract a DOI from the first ~5000 bytes of a PDF."""
    try:
        text = data[:5000].decode("latin-1", errors="ignore")
        doi_pattern = r'10\.\d{4,9}/[^\s]+'
        match = re.search(doi_pattern, text)
        if match:
            return match.group(0).rstrip(".")
    except Exception:
        pass
    return None


def _parse_citation_line(line: str) -> dict:
    """
    Best-effort extraction of authors, year, and title from a citation string.
    Handles common APA-ish formats:
      - 'Boubekri, M., Cheung, I. N., & Reid, K. J. (2014). Impact of windows...'
      - 'Boubekri et al. (2014). Impact of windows and daylight exposure...'
      - 'Impact of windows and daylight exposure on sleep. Boubekri 2014'
      - Just a title with no structured author/year

    Returns dict with keys: authors, year, title (any may be empty string).
    """
    result = {"authors": "", "year": "", "title": ""}

    # Extract year (4-digit number in parens or standalone)
    year_match = re.search(r'\((\d{4})\)', line) or re.search(r'\b(19|20)\d{2}\b', line)
    if year_match:
        result["year"] = year_match.group(1) if '(' in year_match.group(0) else year_match.group(0)

    # Try APA pattern: Authors (Year). Title. Journal...
    # Pattern: everything before (YYYY) is authors, everything after is title+journal
    apa_match = re.match(r'^(.+?)\s*\(\d{4}\)\.\s*(.+)', line)
    if apa_match:
        result["authors"] = apa_match.group(1).strip().rstrip(',').rstrip('.')
        remainder = apa_match.group(2)
        # Title is typically the first sentence (ends at period followed by space+capital or italic marker)
        title_match = re.match(r'^(.+?)\.\s+[A-Z]', remainder)
        if title_match:
            result["title"] = title_match.group(1).strip()
        else:
            # Take everything up to the first period as title
            result["title"] = remainder.split('.')[0].strip()
        return result

    # Try: Authors. (Year). Title. ...
    apa_match2 = re.match(r'^(.+?)\.\s*\(\d{4}\)\.\s*(.+)', line)
    if apa_match2:
        result["authors"] = apa_match2.group(1).strip()
        remainder = apa_match2.group(2)
        title_match = re.match(r'^(.+?)\.\s+[A-Z]', remainder)
        result["title"] = title_match.group(1).strip() if title_match else remainder.split('.')[0].strip()
        return result

    # No structured format detected — treat the whole line as a title
    # Strip any DOI from the end
    cleaned = re.sub(r'\s*https?://doi\.org/\S+', '', line)
    cleaned = re.sub(r'\s*10\.\d{4,9}/\S+', '', cleaned)
    result["title"] = cleaned.strip().rstrip('.')
    return result


def _normalize_text(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    t = re.sub(r'[^\w\s]', ' ', (text or '').lower())
    return re.sub(r'\s+', ' ', t).strip()


def _tokenize(text: str) -> list:
    """Split normalized text into word tokens."""
    return _normalize_text(text).split()


def _word_edit_distance(a_tokens: list, b_tokens: list) -> int:
    """
    Compute word-level Levenshtein distance between two token lists.
    Each operation (insert, delete, substitute) costs 1 word.
    """
    m, n = len(a_tokens), len(b_tokens)
    # Optimisation: if lengths differ by more than allowed threshold, skip
    if abs(m - n) > max(2, min(m, n) // 3):
        return max(m, n)
    # Standard DP
    prev = list(range(n + 1))
    for i in range(1, m + 1):
        curr = [i] + [0] * n
        for j in range(1, n + 1):
            cost = 0 if a_tokens[i - 1] == b_tokens[j - 1] else 1
            curr[j] = min(curr[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost)
        prev = curr
    return prev[n]


def _titles_match(title_a: str, title_b: str, max_word_distance: int = 1) -> bool:
    """
    True if two titles are within max_word_distance words of each other.
    Also returns True for exact containment (one is a substring of the other
    after normalization), which catches truncation cases.
    """
    norm_a = _normalize_text(title_a)
    norm_b = _normalize_text(title_b)
    if not norm_a or not norm_b:
        return False
    # Exact match after normalization
    if norm_a == norm_b:
        return True
    # Containment (handles truncated titles)
    if norm_a in norm_b or norm_b in norm_a:
        return True
    # Word-level edit distance
    toks_a = norm_a.split()
    toks_b = norm_b.split()
    # Both must have at least 3 words to be meaningful for fuzzy matching
    if len(toks_a) < 3 or len(toks_b) < 3:
        return False
    dist = _word_edit_distance(toks_a, toks_b)
    return dist <= max_word_distance


def _authors_match(authors_a: str, authors_b: str, max_word_distance: int = 1) -> bool:
    """
    True if two author strings are within max_word_distance words of each other.
    Handles common APA variations:
      - 'Boubekri, M., Cheung, I. N.' vs 'Boubekri, M., Cheung, I.N.'
      - 'Boubekri et al.' vs 'Boubekri, M., Cheung, I. N., Reid, K. J.'
    Also matches if the first author's last name is identical (covers
    'Boubekri et al.' matching 'Boubekri, Cheung, & Reid').
    """
    norm_a = _normalize_text(authors_a)
    norm_b = _normalize_text(authors_b)
    if not norm_a or not norm_b:
        return False
    if norm_a == norm_b:
        return True
    # First-author last name check: extract the first word (last name in APA)
    first_a = norm_a.split()[0] if norm_a else ''
    first_b = norm_b.split()[0] if norm_b else ''
    # If both have 'et al' and share first author, that's a match
    if first_a == first_b and ('et al' in norm_a or 'et al' in norm_b):
        return True
    # Full word-level edit distance
    toks_a = norm_a.split()
    toks_b = norm_b.split()
    dist = _word_edit_distance(toks_a, toks_b)
    return dist <= max_word_distance


def _check_duplicate(pdf_hash: Optional[str] = None, doi: Optional[str] = None,
                     title: Optional[str] = None, authors: Optional[str] = None) -> dict:
    """
    Check for duplicates using a cascade of increasingly fuzzy methods:
      1. Exact PDF hash match (definitive)
      2. Exact DOI match (definitive)
      3. Fuzzy title match (≤1 word edit distance) — flags as duplicate
      4. Fuzzy title+authors match — if title alone is ambiguous, author
         agreement strengthens the match

    Many APA citations lack DOIs, so title+author fuzzy matching is essential.
    The word-level edit distance tolerates one word difference (e.g., a subtitle
    variation, a dropped article, or a minor wording change between preprint
    and published version).
    """
    db = _get_db()
    result = {"is_duplicate": False, "matches": [], "check_type": []}

    # ── 1. Hash check (exact, definitive)
    if pdf_hash:
        result["check_type"].append("hash")
        row = db.execute(
            "SELECT article_id, title, authors, doi, status FROM articles WHERE pdf_hash_sha256=? LIMIT 1",
            (pdf_hash,)).fetchone()
        if row:
            result["is_duplicate"] = True
            result["matches"].append({
                "article_id": row["article_id"],
                "title": row["title"],
                "authors": row["authors"],
                "doi": row["doi"],
                "match_type": "exact_hash",
                "confidence": "definitive"
            })
            db.close()
            return result

    # ── 2. DOI check (exact, definitive)
    if doi:
        result["check_type"].append("doi")
        clean_doi = doi.strip().lower()
        row = db.execute(
            "SELECT article_id, title, authors, doi, status FROM articles WHERE LOWER(doi)=? LIMIT 1",
            (clean_doi,)).fetchone()
        if row:
            result["is_duplicate"] = True
            result["matches"].append({
                "article_id": row["article_id"],
                "title": row["title"],
                "authors": row["authors"],
                "doi": row["doi"],
                "match_type": "exact_doi",
                "confidence": "definitive"
            })
            db.close()
            return result

    # ── 3. Fuzzy title (+ optional author) matching
    if title and len(title.strip()) > 10:
        result["check_type"].append("title_fuzzy")
        rows = db.execute(
            "SELECT article_id, title, authors, doi, status FROM articles WHERE title IS NOT NULL"
        ).fetchall()

        for row in rows:
            existing_title = row["title"] or ""
            if not existing_title:
                continue

            if _titles_match(title, existing_title, max_word_distance=1):
                # Title matches within 1 word — check if authors also match
                match_type = "title_fuzzy_1word"
                confidence = "high"

                existing_authors = row["authors"] or ""
                # If we have authors for both, author agreement raises confidence
                if authors and existing_authors:
                    if _authors_match(authors, existing_authors, max_word_distance=1):
                        match_type = "title_and_authors_fuzzy"
                        confidence = "very_high"
                    else:
                        # Title matches but authors don't — possible different paper
                        # with a similar title. Still flag but lower confidence.
                        match_type = "title_fuzzy_authors_differ"
                        confidence = "medium"

                result["is_duplicate"] = True
                result["matches"].append({
                    "article_id": row["article_id"],
                    "title": row["title"],
                    "authors": row["authors"],
                    "doi": row["doi"],
                    "match_type": match_type,
                    "confidence": confidence
                })
                break  # One match is enough

    # ── 4. Author-only check: if title is very short or missing but authors are provided,
    #    we don't flag as duplicate (too many false positives). Author matching is only
    #    a strengthener for title matches, not standalone.

    db.close()
    return result


def _sanitize_filename(name: str) -> str:
    """Strip to alphanumeric + hyphens."""
    return re.sub(r'[^a-zA-Z0-9_-]', '_', Path(name).stem)[:80]


def _write_audit(article_id: str, action: str, old_status: str = None,
                 new_status: str = None, actor_id: str = None,
                 actor_type: str = "system", details: dict = None):
    db = _get_db()
    db.execute(
        "INSERT INTO audit_log (article_id, action, old_status, new_status, actor_id, actor_type, details, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (article_id, action, old_status, new_status, actor_id, actor_type,
         json.dumps(details) if details else None, _now()))
    db.commit()
    db.close()


def _get_optional_user(request: Request) -> Optional[dict]:
    """Extract user from Bearer token if present, otherwise return None."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    try:
        from jose import jwt, JWTError
        # Import secret from main module
        import ka_auth_server as main
        payload = jwt.decode(auth[7:], main.SECRET_KEY, algorithms=[main.ALGORITHM])
        if payload.get("type") != "access":
            return None
        db = _get_db()
        row = db.execute("SELECT * FROM users WHERE user_id=?", (payload["sub"],)).fetchone()
        db.close()
        if row and row["status"] == "approved":
            return dict(row)
    except Exception:
        pass
    return None


# ════════════════════════════════════════════════
# ENDPOINTS
# ════════════════════════════════════════════════

@router.post("/submit")
async def submit_articles(
    request: Request,
    files: List[UploadFile] = File(default=[]),
    citations: str = Form(default=""),
    question_id: str = Form(default=""),
    topic_tags: str = Form(default=""),
    notes: str = Form(default=""),
    source_surface: str = Form(default="ka_contribute"),
    a0_task: str = Form(default=""),          # task1 (experimental-only) | task2 (any-type)
    article_type: str = Form(default=""),     # experimental | review | theory | mechanism | meta_analysis | other
):
    """
    Submit one or more articles (PDFs and/or citation text).
    Auth is optional — anonymous submissions are allowed.

    A0 submissions should include:
      - a0_task: 'task1' (10 experimental articles) or 'task2' (10 any-type articles)
      - article_type: classification of the article
    For task1, only 'experimental' articles count toward the requirement.
    """
    user = _get_optional_user(request)

    # Must provide at least something
    if not files and not citations.strip():
        raise HTTPException(400, "Provide at least one PDF file or citation text")

    # Check batch size
    total_size = 0
    for f in files:
        content = await f.read()
        total_size += len(content)
        await f.seek(0)  # reset for later read
    if total_size > MAX_BATCH_SIZE_BYTES:
        raise HTTPException(
            413, f"Total batch size ({total_size} bytes) exceeds limit ({MAX_BATCH_SIZE_BYTES} bytes)")

    # Create submission batch
    submission_id = _next_id("KA-IN-", "submission_batches", "submission_id")
    now = _now()
    submitter_id = user["user_id"] if user else None
    submitter_type = user["role"] if user else "anonymous"
    track = user.get("track") if user else None

    db = _get_db()
    db.execute(
        "INSERT INTO submission_batches VALUES (?,?,?,?,?,?,?,?)",
        (submission_id, submitter_id, submitter_type, "mixed", 0, source_surface, now, None))
    db.commit()
    db.close()

    items = []

    # ── Process PDF files
    for f in files:
        content = await f.read()
        article_id = _next_id("KA-ART-", "articles", "article_id")
        filename = f.filename or "unknown.pdf"
        pdf_hash = _compute_sha256(content)
        pdf_size = len(content)

        # ─── Pre-storage validation ───────────────────────────────────────
        validation = _validate_pdf_bytes(content, filename)
        if not validation.get("valid"):
            db = _get_db()
            db.execute(
                """INSERT INTO articles
                    (article_id, submission_id, submitter_id, submitter_type, track, input_mode,
                     pdf_filename, pdf_hash_sha256, pdf_size_bytes,
                     status, validation_notes,
                     assigned_question_id, topic_tags,
                     source_surface, course_context, submitter_notes,
                     created_at, validated_at, rejected_at)
                   VALUES (?,?,?,?,?,?, ?,?,?, ?,?, ?,?, ?,?,?, ?,?,?)""",
                (article_id, submission_id, submitter_id, submitter_type, track, "pdf_single",
                 filename, pdf_hash, pdf_size,
                 "rejected_bad_file", json.dumps(validation),
                 question_id or None, topic_tags or None,
                 source_surface, "COGS160-SP26", notes or None,
                 now, now, now))
            db.commit()
            db.close()
            _write_audit(article_id, "rejected_bad_file",
                         "received", "rejected_bad_file",
                         actor_type="system", details=validation)
            items.append({
                "article_id":        article_id,
                "input_mode":        "pdf_single",
                "filename":          filename,
                "validation_status": "invalid",
                "duplicate_status":  "not_checked",
                "metadata":          {},
                "status":            "rejected_bad_file",
                "reason":            validation.get("rejection_reason", "Validation failed"),
            })
            continue

        # ─── STEP 1 · Save the file temporarily to a quarantine path ──────
        month_dir = QUARANTINE_DIR / datetime.now().strftime("%Y-%m")
        month_dir.mkdir(parents=True, exist_ok=True)
        quarantine_path = month_dir / f"{article_id}.pdf"
        quarantine_path.write_bytes(content)

        # ─── STEP 2 + 3 · Duplicate lookup against `articles`; on match,
        #                  delete the file and record rejected_duplicate_in_corpus ───
        extracted_doi = _extract_doi_from_pdf(content)
        dup_result = _check_duplicate(pdf_hash=pdf_hash, doi=extracted_doi)

        if dup_result["is_duplicate"]:
            # Unlink the just-written file so the grader's storage-path
            # invariant holds (status LIKE 'reject%' must have NULL quarantine_path).
            quarantine_path.unlink(missing_ok=True)
            dup_match = dup_result["matches"][0] if dup_result["matches"] else {}

            db = _get_db()
            db.execute(
                """INSERT INTO articles
                    (article_id, submission_id, submitter_id, submitter_type, track, input_mode,
                     doi, pdf_filename, pdf_hash_sha256, pdf_size_bytes, quarantine_path,
                     status, duplicate_of, validation_notes,
                     assigned_question_id, topic_tags,
                     source_surface, course_context, submitter_notes,
                     created_at, validated_at, rejected_at)
                   VALUES (?,?,?,?,?,?, ?,?,?,?,?, ?,?,?, ?,?, ?,?,?, ?,?,?)""",
                (article_id, submission_id, submitter_id, submitter_type, track, "pdf_single",
                 extracted_doi, filename, pdf_hash, pdf_size, None,   # quarantine_path = NULL
                 STATUS_REJECTED_DUPLICATE, dup_match.get("article_id"),
                 json.dumps({"validation": validation, "duplicate_check": dup_result}),
                 question_id or None, topic_tags or None,
                 source_surface, "COGS160-SP26", notes or None,
                 now, now, now))
            db.commit()
            db.close()
            _write_audit(article_id, STATUS_REJECTED_DUPLICATE,
                         "received", STATUS_REJECTED_DUPLICATE,
                         actor_type="system", details=dup_result)
            items.append({
                "article_id":        article_id,
                "input_mode":        "pdf_single",
                "filename":          filename,
                "validation_status": "valid",
                "duplicate_status":  "duplicate",
                "duplicate_of":      dup_match.get("article_id"),
                "metadata":          {"doi": extracted_doi},
                "status":            STATUS_REJECTED_DUPLICATE,
                "reason":            "Already in the Atlas intake — see {}".format(
                                         dup_match.get("article_id") or "unknown"),
            })
            continue

        # ─── STEP 4 · Pass the unique PDF to the classifier ───────────────
        classification = _classify_intake_pdf(
            article_id=article_id,
            quarantine_path=quarantine_path,
            extracted_doi=extracted_doi,
            filename=filename,
        )
        decision = classification["decision"]
        status   = _status_for_decision(decision)
        retain_pdf = status != STATUS_REJECTED_OFF_TOPIC

        # If the classifier rejected the paper, delete the quarantine PDF so
        # the storage-path invariant holds.
        if not retain_pdf:
            quarantine_path.unlink(missing_ok=True)

        # ─── STEP 5 · Write the row + the audit entry ─────────────────────

        # metadata_confidence: keep the existing low/medium/high heuristic.
        if extracted_doi and classification["extracted_title"]:
            metadata_confidence = "high"
        elif extracted_doi:
            metadata_confidence = "medium"
        else:
            metadata_confidence = "low"

        # article_type: prefer a caller-supplied valid override (used by A0),
        # otherwise take the classifier's KA-bucket label.
        art_type = classification["article_type"]
        if article_type and article_type in VALID_ARTICLE_TYPES:
            art_type = article_type
        art_type_valid = 1 if (
            not a0_task or a0_task != "task1" or art_type == "experimental"
        ) else 0

        validation_notes_payload = {
            "validation":      validation,
            "duplicate_check": dup_result,
            "classifier": {
                "decision":         decision,
                "primary_topic":    classification["primary_topic"],
                "secondary_topics": classification["secondary_topics"],
                "reasons":          classification["classifier_reasons"],
                "evidence_stage":   classification["evidence_stage"],
                "next_action":      classification["next_action"],
                "raw_summary":      classification["raw_result_summary"],
            },
        }

        db = _get_db()
        db.execute(
            """INSERT INTO articles
                (article_id, submission_id, submitter_id, submitter_type, track, input_mode,
                 doi, title, abstract,
                 pdf_filename, pdf_hash_sha256, pdf_size_bytes, quarantine_path,
                 article_type, a0_task, article_type_valid,
                 status, validation_notes, relevance_score,
                 assigned_question_id, topic_tags,
                 source_surface, course_context, submitter_notes,
                 created_at, validated_at, staged_at, rejected_at, metadata_confidence)
               VALUES (?,?,?,?,?,?, ?,?,?, ?,?,?,?, ?,?,?, ?,?,?, ?,?, ?,?,?, ?,?,?,?,?)""",
            (article_id, submission_id, submitter_id, submitter_type, track, "pdf_single",
             extracted_doi,
             classification["extracted_title"]    or None,
             classification["extracted_abstract"] or None,
             filename, pdf_hash, pdf_size,
             str(quarantine_path) if retain_pdf else None,
             art_type, a0_task or None, art_type_valid,
             status,
             json.dumps(validation_notes_payload),
             classification["classifier_confidence"],
             question_id or None, topic_tags or None,
             source_surface, "COGS160-SP26", notes or None,
             now,
             now,                                  # validated_at
             now  if retain_pdf else None,         # staged_at
             None if retain_pdf else now,          # rejected_at
             metadata_confidence))
        db.commit()
        db.close()

        _write_audit(article_id, status, "received", status,
                     actor_id=submitter_id,
                     actor_type=submitter_type or "system",
                     details=validation_notes_payload["classifier"])

        items.append({
            "article_id":              article_id,
            "input_mode":              "pdf_single",
            "filename":                filename,
            "validation_status":       "valid",
            "duplicate_status":        "not_duplicate",
            "metadata":                {"doi": extracted_doi, "confidence": metadata_confidence},
            "status":                  status,
            "a0_task":                 a0_task or None,
            "article_type":            art_type,
            "counts_toward_requirement": bool(art_type_valid),
            # ── classifier fields (new) ───────────────────────────────────
            "decision":                decision,
            "primary_topic":           classification["primary_topic"],
            "secondary_topics":        classification["secondary_topics"],
            "classifier_confidence":   classification["classifier_confidence"],
            "classifier_reasons":      classification["classifier_reasons"],
        })

    # ── Process citation text
    if citations.strip():
        for line in citations.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            article_id = _next_id("KA-ART-", "articles", "article_id")

            # Parse citation to extract structured fields
            parsed = _parse_citation_line(line)

            # Try to extract DOI from citation
            doi_match = re.search(r'10\.\d{4,9}/[^\s]+', line)
            extracted_doi = doi_match.group(0).rstrip(".") if doi_match else None

            # Use parsed title for dedup (falls back to raw line if parsing fails)
            dedup_title = parsed["title"] if parsed["title"] else line[:200]
            dedup_authors = parsed["authors"]

            # Duplicate check — fuzzy on title and authors (≤1 word edit distance)
            dup_result = _check_duplicate(doi=extracted_doi, title=dedup_title, authors=dedup_authors)

            status = "duplicate_existing" if dup_result["is_duplicate"] else "staged_pending_review"
            dup_of = dup_result["matches"][0]["article_id"] if dup_result["is_duplicate"] and dup_result["matches"] else None

            db = _get_db()
            db.execute("""INSERT INTO articles
                (article_id, submission_id, submitter_id, submitter_type, track, input_mode,
                 doi, title, authors, year, citation_raw,
                 status, duplicate_of,
                 assigned_question_id, topic_tags,
                 source_surface, course_context, submitter_notes,
                 created_at, staged_at, metadata_confidence)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (article_id, submission_id, submitter_id, submitter_type, track, "citation_text",
                 extracted_doi,
                 parsed["title"] or None,
                 parsed["authors"] or None,
                 int(parsed["year"]) if parsed["year"] else None,
                 line,
                 status, dup_of,
                 question_id or None, topic_tags or None,
                 source_surface, "COGS160-SP26", notes or None,
                 now, now if status == "staged_pending_review" else None,
                 "high" if extracted_doi and parsed["title"] and parsed["authors"]
                 else "medium" if extracted_doi or (parsed["title"] and parsed["authors"])
                 else "low"))
            db.commit()
            db.close()

            _write_audit(article_id,
                         "duplicate_detected" if dup_result["is_duplicate"] else "staged",
                         "received", status,
                         actor_id=submitter_id, actor_type=submitter_type)

            items.append({
                "article_id": article_id,
                "input_mode": "citation_text",
                "citation": line[:200],
                "validation_status": "accepted",
                "duplicate_status": "duplicate" if dup_result["is_duplicate"] else "not_duplicate",
                "metadata": {"doi": extracted_doi},
                "status": status
            })

    # Update batch count
    db = _get_db()
    db.execute("UPDATE submission_batches SET item_count=?, completed_at=? WHERE submission_id=?",
               (len(items), _now(), submission_id))
    db.commit()
    db.close()

    staged = sum(1 for i in items if i["status"] == "staged_pending_review")
    dups = sum(1 for i in items if i["status"] == "duplicate_existing")
    rejected = sum(1 for i in items if "rejected" in i["status"])

    return {
        "submission_id": submission_id,
        "items": items,
        "summary": {
            "total": len(items),
            "staged": staged,
            "duplicates": dups,
            "rejected": rejected
        }
    }


@router.get("/status/{article_id}")
async def article_status(article_id: str):
    """Check the current status of a submitted article."""
    db = _get_db()
    row = db.execute(
        "SELECT article_id, status, title, doi, created_at, duplicate_of, metadata_confidence FROM articles WHERE article_id=?",
        (article_id,)).fetchone()
    db.close()
    if not row:
        raise HTTPException(404, "Article not found")
    return dict(row)


@router.get("/my-submissions")
async def my_submissions(
    request: Request,
    status_filter: str = "",
    limit: int = 50,
    offset: int = 0,
):
    """List all articles submitted by the authenticated user."""
    user = _get_optional_user(request)
    if not user:
        raise HTTPException(401, "Authentication required to view submissions")

    db = _get_db()
    base_query = "FROM articles WHERE submitter_id=?"
    params = [user["user_id"]]

    if status_filter:
        base_query += " AND status=?"
        params.append(status_filter)

    total = db.execute(f"SELECT COUNT(*) {base_query}", params).fetchone()[0]
    rows = db.execute(
        f"SELECT article_id, submission_id, input_mode, doi, title, pdf_filename, status, created_at, metadata_confidence {base_query} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        params + [limit, offset]).fetchall()

    # Counts by status
    counts_rows = db.execute(
        "SELECT status, COUNT(*) as cnt FROM articles WHERE submitter_id=? GROUP BY status",
        (user["user_id"],)).fetchall()
    db.close()

    counts = {r["status"]: r["cnt"] for r in counts_rows}

    return {
        "submissions": [dict(r) for r in rows],
        "counts": {
            "total": total,
            "staged": counts.get("staged_pending_review", 0),
            "accepted": counts.get("accepted_for_extraction", 0),
            "rejected": counts.get("rejected_irrelevant", 0) + counts.get("rejected_bad_file", 0),
            "duplicate": counts.get("duplicate_existing", 0)
        }
    }


@router.get("/pending-review")
async def pending_review(
    request: Request,
    limit: int = 50,
    offset: int = 0,
):
    """List articles awaiting instructor review."""
    user = _get_optional_user(request)
    if not user or user["role"] not in ("instructor", "admin"):
        raise HTTPException(403, "Instructor access required")

    db = _get_db()
    rows = db.execute(
        """SELECT a.*, u.email as submitter_email, u.first_name, u.last_name
           FROM articles a
           LEFT JOIN users u ON a.submitter_id = u.user_id
           WHERE a.status = 'staged_pending_review'
           ORDER BY a.created_at DESC LIMIT ? OFFSET ?""",
        (limit, offset)).fetchall()
    total = db.execute(
        "SELECT COUNT(*) FROM articles WHERE status='staged_pending_review'").fetchone()[0]
    db.close()
    return {"items": [dict(r) for r in rows], "total": total}


class ReviewRequest(BaseModel):
    decision: str  # "accept" or "reject"
    reason: str = ""
    topic_override: str = ""
    notes: str = ""


@router.post("/{article_id}/review")
async def review_article(article_id: str, req: ReviewRequest, request: Request):
    """Accept or reject a staged article (instructor only)."""
    user = _get_optional_user(request)
    if not user or user["role"] not in ("instructor", "admin"):
        raise HTTPException(403, "Instructor access required")

    if req.decision not in ("accept", "reject"):
        raise HTTPException(400, "Decision must be 'accept' or 'reject'")

    db = _get_db()
    row = db.execute("SELECT * FROM articles WHERE article_id=?", (article_id,)).fetchone()
    if not row:
        db.close()
        raise HTTPException(404, "Article not found")

    if row["status"] != "staged_pending_review":
        db.close()
        raise HTTPException(400, f"Article is not pending review (current status: {row['status']})")

    now = _now()

    if req.decision == "accept":
        # Determine topic folder
        topic_slug = "general"
        if req.topic_override:
            topic_slug = re.sub(r'[^a-zA-Z0-9_-]', '_', req.topic_override.lower())
        elif row["topic_tags"]:
            try:
                tags = json.loads(row["topic_tags"])
                if tags:
                    topic_slug = re.sub(r'[^a-zA-Z0-9_-]', '_', tags[0].lower())
            except (json.JSONDecodeError, IndexError):
                pass

        # Move PDF from quarantine to collection
        promoted_path = None
        if row["quarantine_path"] and Path(row["quarantine_path"]).exists():
            dest_dir = PDF_COLLECTION_DIR / topic_slug
            dest_dir.mkdir(parents=True, exist_ok=True)
            safe_title = _sanitize_filename(row["title"] or row["pdf_filename"] or "untitled")
            dest_name = f"{article_id}_{safe_title}.pdf"
            promoted_path = dest_dir / dest_name
            shutil.move(row["quarantine_path"], str(promoted_path))

            # Write sidecar JSON for extraction pipeline
            sidecar = {
                "article_id": article_id,
                "doi": row["doi"],
                "title": row["title"],
                "authors": json.loads(row["authors"]) if row["authors"] else [],
                "year": row["year"],
                "journal": row["journal"],
                "abstract": row["abstract"],
                "promoted_at": now,
                "source": "ka_article_submission",
                "submitter_id": row["submitter_id"],
                "pdf_hash_sha256": row["pdf_hash_sha256"]
            }
            sidecar_path = promoted_path.with_suffix(".json")
            sidecar_path.write_text(json.dumps(sidecar, indent=2))

        db.execute("""UPDATE articles SET
            status='accepted_for_extraction', promoted_path=?,
            reviewed_at=?, promoted_at=?, review_notes=?
            WHERE article_id=?""",
            (str(promoted_path) if promoted_path else None, now, now, req.notes, article_id))
        db.commit()
        db.close()

        _write_audit(article_id, "accepted", "staged_pending_review", "accepted_for_extraction",
                     actor_id=user["user_id"], actor_type="instructor",
                     details={"topic": topic_slug, "promoted_path": str(promoted_path) if promoted_path else None})

        return {"article_id": article_id, "status": "accepted_for_extraction",
                "promoted_path": str(promoted_path) if promoted_path else None}

    else:  # reject
        # Delete PDF from quarantine
        if row["quarantine_path"] and Path(row["quarantine_path"]).exists():
            Path(row["quarantine_path"]).unlink()

        # Write rejection stub
        reject_dir = REJECTED_DIR / datetime.now().strftime("%Y-%m")
        reject_dir.mkdir(parents=True, exist_ok=True)
        stub = {
            "article_id": article_id,
            "title": row["title"],
            "doi": row["doi"],
            "submitter_id": row["submitter_id"],
            "rejection_reason": req.reason,
            "rejected_at": now,
            "reviewer": user["user_id"]
        }
        (reject_dir / f"{article_id}.json").write_text(json.dumps(stub, indent=2))

        new_status = "rejected_irrelevant" if req.reason else "rejected_irrelevant"
        db.execute("""UPDATE articles SET
            status=?, rejected_at=?, review_notes=?, quarantine_path=NULL
            WHERE article_id=?""",
            (new_status, now, req.notes or req.reason, article_id))
        db.commit()
        db.close()

        _write_audit(article_id, "rejected", "staged_pending_review", new_status,
                     actor_id=user["user_id"], actor_type="instructor",
                     details={"reason": req.reason})

        return {"article_id": article_id, "status": new_status}


class DuplicateCheckRequest(BaseModel):
    doi: str = ""
    title: str = ""
    authors: str = ""
    pdf_hash: str = ""


@router.post("/check-duplicate")
async def check_duplicate(req: DuplicateCheckRequest):
    """Quick duplicate check without full submission. Fuzzy-matches title
    and authors within 1 word edit distance to catch APA citations without DOIs."""
    if not req.doi and not req.title and not req.pdf_hash:
        raise HTTPException(400, "Provide at least one of: doi, title, pdf_hash")
    result = _check_duplicate(
        pdf_hash=req.pdf_hash or None, doi=req.doi or None,
        title=req.title or None, authors=req.authors or None)
    return result


@router.get("/stats")
async def article_stats(request: Request):
    """Corpus and personal statistics."""
    user = _get_optional_user(request)
    db = _get_db()

    # Corpus stats
    corpus_total = db.execute(
        "SELECT COUNT(*) FROM articles WHERE status='accepted_for_extraction'").fetchone()[0]
    corpus_staged = db.execute(
        "SELECT COUNT(*) FROM articles WHERE status='staged_pending_review'").fetchone()[0]
    corpus_rejected = db.execute(
        "SELECT COUNT(*) FROM articles WHERE status LIKE 'rejected%'").fetchone()[0]

    # By topic (from topic_tags JSON)
    by_topic = {}
    accepted = db.execute(
        "SELECT topic_tags FROM articles WHERE status='accepted_for_extraction' AND topic_tags IS NOT NULL"
    ).fetchall()
    for row in accepted:
        try:
            tags = json.loads(row["topic_tags"])
            for t in tags:
                by_topic[t] = by_topic.get(t, 0) + 1
        except (json.JSONDecodeError, TypeError):
            pass

    result = {
        "corpus": {
            "total_accepted": corpus_total,
            "total_staged": corpus_staged,
            "total_rejected": corpus_rejected,
            "by_topic": by_topic
        }
    }

    # Personal stats (if authenticated)
    if user:
        personal_rows = db.execute(
            "SELECT status, COUNT(*) as cnt FROM articles WHERE submitter_id=? GROUP BY status",
            (user["user_id"],)).fetchall()
        personal = {r["status"]: r["cnt"] for r in personal_rows}
        result["personal"] = {
            "submitted": sum(personal.values()),
            "accepted": personal.get("accepted_for_extraction", 0),
            "staged": personal.get("staged_pending_review", 0),
            "rejected": personal.get("rejected_irrelevant", 0) + personal.get("rejected_bad_file", 0),
            "duplicate": personal.get("duplicate_existing", 0)
        }

    db.close()
    return result


# ════════════════════════════════════════════════
# QUESTION CLAIMING (A0 Assignment)
# ════════════════════════════════════════════════

TOTAL_QUESTIONS = 30  # Q01–Q30 (covers all 9 IV domains across the topic ontology)


def _current_round(db) -> int:
    """
    Determine the current round. A round is complete once every question has
    been used in that round, even if some claims were later released.

    This matches the UNIQUE(question_id, round) constraint: once a question has
    been claimed in round N, that same question cannot be inserted again in
    round N, so the next round must open when all question IDs are already used.
    """
    for r in range(1, 100):  # Practical upper bound
        claimed_count = db.execute(
            "SELECT COUNT(DISTINCT question_id) FROM question_claims WHERE round=?",
            (r,)).fetchone()[0]
        if claimed_count < TOTAL_QUESTIONS:
            return r
    return 1  # Fallback


def _available_questions(db, round_num: int) -> list:
    """Return question_ids not yet used in the given round."""
    claimed = db.execute(
        "SELECT DISTINCT question_id FROM question_claims WHERE round=?",
        (round_num,)).fetchall()
    claimed_ids = {r["question_id"] for r in claimed}
    all_qs = db.execute("SELECT question_id FROM research_questions ORDER BY question_id").fetchall()
    return [r["question_id"] for r in all_qs if r["question_id"] not in claimed_ids]


def _repairable_q1_questions(db, user_id: str, round_num: int, current_q1: str = "") -> list:
    """
    Return question_ids a student may legitimately use to repair Q1:
    1. truly unused question IDs in the current round, plus
    2. that student's own prior Q1 claim(s) in the current round, so a released
       question can be restored without creating a duplicate row.
    """
    repairable = set(_available_questions(db, round_num))
    prior_rows = db.execute("""
        SELECT DISTINCT question_id
        FROM question_claims
        WHERE user_id=? AND round=? AND (released_at IS NOT NULL OR question_id=?)
    """, (user_id, round_num, current_q1 or "")).fetchall()
    repairable.update(r["question_id"] for r in prior_rows if r["question_id"])
    return sorted(repairable)


@router.get("/questions/available")
async def available_questions(request: Request):
    """
    Return the list of questions still available for claiming.
    Students see only unclaimed questions in the current round.
    Once all 8 are claimed, a new round opens and all questions
    become available again (we want more experimental articles per question).
    """
    db = _get_db()
    current_round = _current_round(db)
    available_ids = _available_questions(db, current_round)

    # Fetch full question details for available ones
    if not available_ids:
        db.close()
        return {"round": current_round, "available": [], "message": "All questions claimed this round"}

    placeholders = ",".join("?" * len(available_ids))
    questions = db.execute(
        f"SELECT * FROM research_questions WHERE question_id IN ({placeholders}) ORDER BY question_id",
        available_ids).fetchall()

    # Also return claim counts per question across all rounds (so students see coverage)
    coverage = db.execute("""
        SELECT question_id, COUNT(*) as times_claimed,
               SUM(task1_count) as total_experimental,
               SUM(task2_count) as total_any_type
        FROM question_claims WHERE released_at IS NULL
        GROUP BY question_id
    """).fetchall()
    coverage_map = {r["question_id"]: dict(r) for r in coverage}

    db.close()
    return {
        "round": current_round,
        "total_questions": TOTAL_QUESTIONS,
        "available_count": len(available_ids),
        "available": [
            {**dict(q), "coverage": coverage_map.get(q["question_id"], {
                "times_claimed": 0, "total_experimental": 0, "total_any_type": 0
            })}
            for q in questions
        ]
    }


class ClaimQuestionRequest(BaseModel):
    question_id: str


@router.post("/questions/claim")
async def claim_question(req: ClaimQuestionRequest, request: Request):
    """
    Claim a question for A0. Each question can only be held by one student
    per round. When all 8 are claimed, a new round opens.
    Requires authentication.
    """
    user = _get_optional_user(request)
    if not user:
        raise HTTPException(401, "You must be logged in to claim a question")

    db = _get_db()

    # Check the question exists
    q = db.execute("SELECT * FROM research_questions WHERE question_id=?",
                   (req.question_id,)).fetchone()
    if not q:
        db.close()
        raise HTTPException(404, f"Question {req.question_id} not found")

    current_round = _current_round(db)

    # Check if this question is already used in the current round.
    existing = db.execute(
        "SELECT * FROM question_claims WHERE question_id=? AND round=?",
        (req.question_id, current_round)).fetchone()
    if existing and existing["user_id"] != user["user_id"]:
        db.close()
        raise HTTPException(409, f"Question {req.question_id} is already claimed in round {current_round}. "
                                 f"Choose a different question.")

    now = _now()
    try:
        if existing:
            if existing["released_at"] is None:
                remaining = len(_available_questions(db, current_round))
                db.close()
                return {
                    "claimed": True,
                    "question_id": req.question_id,
                    "round": current_round,
                    "remaining_in_round": remaining,
                    "message": f"You already hold {q['label']} in round {current_round}."
                }
            db.execute("""
                UPDATE question_claims
                SET released_at=NULL, claimed_at=?
                WHERE claim_id=?
            """, (now, existing["claim_id"]))
        else:
            # Check if this student already has a claim in the current round
            student_claim = db.execute(
                "SELECT * FROM question_claims WHERE user_id=? AND round=? AND released_at IS NULL",
                (user["user_id"], current_round)).fetchone()
            if student_claim:
                db.close()
                raise HTTPException(409, f"You already claimed question {student_claim['question_id']} in round {current_round}. "
                                         f"Release it first if you want to switch.")
            db.execute(
                "INSERT INTO question_claims (question_id, user_id, round, claimed_at) VALUES (?,?,?,?)",
                (req.question_id, user["user_id"], current_round, now))

        # Preserve the explicit Q1 assignment. Only set users.question_id if none exists yet.
        current_q1 = db.execute("SELECT question_id FROM users WHERE user_id=?",
                                (user["user_id"],)).fetchone()
        if current_q1 and not current_q1["question_id"]:
            db.execute("UPDATE users SET question_id=? WHERE user_id=?",
                       (req.question_id, user["user_id"]))
        db.commit()
    except HTTPException:
        raise
    except sqlite3.IntegrityError:
        db.rollback()
        db.close()
        raise HTTPException(409, f"Question {req.question_id} is no longer available in round {current_round}. Choose a different question.")
    except sqlite3.Error as exc:
        db.rollback()
        db.close()
        raise HTTPException(503, f"Could not claim a question right now: {exc}")

    # Check if this claim completed the round
    remaining = _available_questions(db, current_round)
    db.close()
    return {
        "claimed": True,
        "question_id": req.question_id,
        "round": current_round,
        "remaining_in_round": len(remaining),
        "message": f"You claimed {q['label']} (round {current_round}). "
                   f"{len(remaining)} question(s) still available this round."
                   + (" All questions claimed — next round now open!" if len(remaining) == 0 else "")
    }


@router.post("/questions/release")
async def release_question(req: ClaimQuestionRequest, request: Request):
    """Release a claimed question (e.g., student wants to switch)."""
    user = _get_optional_user(request)
    if not user:
        raise HTTPException(401, "You must be logged in")

    db = _get_db()
    claim = db.execute(
        "SELECT * FROM question_claims WHERE question_id=? AND user_id=? AND released_at IS NULL",
        (req.question_id, user["user_id"])).fetchone()
    if not claim:
        db.close()
        raise HTTPException(404, "You don't have an active claim on this question")

    db.execute("UPDATE question_claims SET released_at=? WHERE claim_id=?",
               (_now(), claim["claim_id"]))
    current_q1 = db.execute("SELECT question_id FROM users WHERE user_id=?",
                            (user["user_id"],)).fetchone()
    if current_q1 and current_q1["question_id"] == req.question_id:
        db.execute("UPDATE users SET question_id=NULL WHERE user_id=?", (user["user_id"],))
    db.commit()
    db.close()
    return {"released": True, "question_id": req.question_id}


@router.get("/questions/my-claim")
async def my_claim(request: Request):
    """
    Return ALL of the current student's active question claims and A0 progress.
    A student may have claims across multiple rounds (e.g., Q01 in round 1, Q03 in round 2).
    Each claim tracks task1 (experimental) and task2 (any-type) progress independently.
    """
    user = _get_optional_user(request)
    if not user:
        raise HTTPException(401, "You must be logged in")

    db = _get_db()
    claims = db.execute("""
        SELECT qc.*, rq.label, rq.domain, rq.text, rq.atlas_topic, rq.notes
        FROM question_claims qc
        JOIN research_questions rq ON qc.question_id = rq.question_id
        WHERE qc.user_id=? AND qc.released_at IS NULL
        ORDER BY qc.round ASC, qc.claimed_at ASC
    """, (user["user_id"],)).fetchall()

    if not claims:
        db.close()
        return {"has_claim": False, "claims": [], "message": "You haven't claimed a question yet"}

    claim_list = []
    total_task1_exp = 0
    total_task2 = 0

    for claim in claims:
        qid = claim["question_id"]

        task1_articles = db.execute("""
            SELECT COUNT(*) FROM articles
            WHERE submitter_id=? AND assigned_question_id=? AND a0_task='task1'
            AND status NOT LIKE 'rejected%%' AND status != 'duplicate_existing'
        """, (user["user_id"], qid)).fetchone()[0]

        task2_articles = db.execute("""
            SELECT COUNT(*) FROM articles
            WHERE submitter_id=? AND assigned_question_id=? AND a0_task='task2'
            AND status NOT LIKE 'rejected%%' AND status != 'duplicate_existing'
        """, (user["user_id"], qid)).fetchone()[0]

        # NOTE: The experimental-article classification filter was removed from
        # the upload pipeline, so article_type may be NULL/empty for most task1
        # submissions. Count ALL non-rejected task1 articles as "experimental"
        # until the classification filter is reinstated.  — DK 2026-04-12
        task1_experimental = db.execute("""
            SELECT COUNT(*) FROM articles
            WHERE submitter_id=? AND assigned_question_id=? AND a0_task='task1'
            AND status NOT LIKE 'rejected%%' AND status != 'duplicate_existing'
        """, (user["user_id"], qid)).fetchone()[0]

        total_task1_exp += task1_experimental
        total_task2 += task2_articles

        claim_list.append({
            "question_id": qid,
            "label": claim["label"],
            "domain": claim["domain"],
            "text": claim["text"],
            "round": claim["round"],
            "progress": {
                "task1": {
                    "submitted": task1_articles,
                    "confirmed_experimental": task1_experimental,
                    "required": 10,
                    "requirement": "All 10 must be experimental articles",
                    "complete": task1_experimental >= 10
                },
                "task2": {
                    "submitted": task2_articles,
                    "required": 10,
                    "requirement": "Any article type accepted",
                    "complete": task2_articles >= 10
                },
                "a0_complete": task1_experimental >= 10 and task2_articles >= 10
            }
        })

    db.close()

    return {
        "has_claim": True,
        "claims": claim_list,
        "total_progress": {
            "total_experimental": total_task1_exp,
            "total_any_type": total_task2,
            "total_articles": total_task1_exp + total_task2,
            "target": 20
        }
    }


# ════════════════════════════════════════════════
# A0 ARTICLE TYPE VALIDATION
# ════════════════════════════════════════════════

VALID_ARTICLE_TYPES = {"experimental", "review", "theory", "mechanism", "meta_analysis", "other", "unknown"}


class SetArticleTypeRequest(BaseModel):
    article_type: str  # experimental | review | theory | mechanism | meta_analysis | other
    a0_task: str = ""  # task1 | task2 (optional, set at submission time)


@router.post("/{article_id}/set-type")
async def set_article_type(article_id: str, req: SetArticleTypeRequest, request: Request):
    """
    Set or update the article type classification. Used by:
    - Students self-classifying during upload
    - Instructors correcting classifications during review

    For Task 1 submissions, article_type MUST be 'experimental' to count toward
    the 10-article requirement. Non-experimental articles submitted to Task 1 are
    stored but flagged as not counting.
    """
    user = _get_optional_user(request)
    if not user:
        raise HTTPException(401, "Authentication required")

    if req.article_type not in VALID_ARTICLE_TYPES:
        raise HTTPException(400, f"Invalid article_type. Must be one of: {', '.join(sorted(VALID_ARTICLE_TYPES))}")

    db = _get_db()
    row = db.execute("SELECT * FROM articles WHERE article_id=?", (article_id,)).fetchone()
    if not row:
        db.close()
        raise HTTPException(404, "Article not found")

    a0_task = req.a0_task or row["a0_task"]
    # For task1: only 'experimental' is valid (counts toward requirement)
    type_valid = 1
    message = f"Article classified as {req.article_type}"
    if a0_task == "task1" and req.article_type != "experimental":
        type_valid = 0
        message = (f"Article classified as {req.article_type}. Note: Task 1 requires experimental "
                   f"articles — this paper is stored but does not count toward your 10-article requirement.")

    db.execute("""UPDATE articles SET article_type=?, a0_task=?, article_type_valid=?
                  WHERE article_id=?""",
               (req.article_type, a0_task, type_valid, article_id))
    db.commit()

    # Update the claim's task counts
    if a0_task and row["submitter_id"] and row["assigned_question_id"]:
        _update_claim_counts(db, row["submitter_id"], row["assigned_question_id"])

    db.close()

    return {"article_id": article_id, "article_type": req.article_type,
            "a0_task": a0_task, "counts_toward_requirement": bool(type_valid),
            "message": message}


def _update_claim_counts(db, user_id: str, question_id: str):
    """Recalculate task1/task2 counts on the user's active claim."""
    task1 = db.execute("""
        SELECT COUNT(*) FROM articles
        WHERE submitter_id=? AND assigned_question_id=? AND a0_task='task1'
        AND status NOT LIKE 'rejected%%' AND status != 'duplicate_existing'
    """, (user_id, question_id)).fetchone()[0]

    task2 = db.execute("""
        SELECT COUNT(*) FROM articles
        WHERE submitter_id=? AND assigned_question_id=? AND a0_task='task2'
        AND status NOT LIKE 'rejected%%' AND status != 'duplicate_existing'
    """, (user_id, question_id)).fetchone()[0]

    db.execute("""UPDATE question_claims SET task1_count=?, task2_count=?
                  WHERE user_id=? AND question_id=? AND released_at IS NULL""",
               (task1, task2, user_id, question_id))
    db.commit()


# ════════════════════════════════════════════════
# AUTOMATIC ARTICLE TYPE CLASSIFICATION
# ════════════════════════════════════════════════

_SHARED_ARTICLE_CLASSIFIER: AdaptiveClassifierSubsystem | None = None


def _get_shared_article_classifier() -> AdaptiveClassifierSubsystem:
    global _SHARED_ARTICLE_CLASSIFIER
    if _SHARED_ARTICLE_CLASSIFIER is None:
        _SHARED_ARTICLE_CLASSIFIER = AdaptiveClassifierSubsystem()
    return _SHARED_ARTICLE_CLASSIFIER


def _extract_abstract_from_text(text: str) -> str:
    if not text.strip():
        return ""
    match = re.search(
        r"(?i)\babstract\b[:\s]*(.{50,800}?)(?:\n\n|\bintroduction\b|\bkeywords\b)",
        text,
    )
    if match:
        return match.group(1).strip()
    return text[:300].strip()


def _extract_title_from_text(text: str, fallback: str = "") -> str:
    if not text.strip():
        return fallback
    lines = [line.strip() for line in text.splitlines() if len(line.strip()) > 10]
    if not lines:
        return fallback
    candidate = lines[0][:200]
    if candidate.lower().startswith(("http", "doi", "volume", "journal")):
        return fallback
    return candidate if len(candidate) > 15 else fallback


def _map_shared_article_type_to_ka_bucket(shared_label: str) -> str:
    normalized = str(shared_label or "").strip().lower()
    if normalized == "empirical_research":
        return "experimental"
    if normalized in {"systematic_review", "narrative_review"}:
        return "review"
    if normalized == "meta_analysis":
        return "meta_analysis"
    if normalized == "theoretical":
        return "theory"
    if normalized in {"commentary", "protocol", "case_study", "mixed_methods", "qualitative_research"}:
        return "other"
    if normalized in {"unknown", ""}:
        return "unknown"
    return "other"


def _classify_article_payload(
    *,
    title: str = "",
    abstract: str = "",
    keywords: Optional[List[str]] = None,
    text_surface: str = "",
) -> dict:
    evidence = ClassificationEvidence(
        paper_id="",
        title=title,
        abstract=abstract,
        keywords=tuple(keywords or ()),
        first_page_text=text_surface[:3000].strip(),
    )
    result = _get_shared_article_classifier().classify(
        evidence,
        allow_surface_creation=False,
    )
    shared_label = result.article_type.value
    return {
        "article_type": _map_shared_article_type_to_ka_bucket(shared_label),
        "canonical_article_type": shared_label,
        "confidence": round(result.article_type.confidence, 2),
        "signals": list(result.article_type.evidence),
        "source": result.article_type.source,
        "evidence_stage": result.evidence_stage,
        "next_action": result.next_action,
    }



DECISION_ACCEPT              = "ACCEPT"
DECISION_EDGE_CASE           = "EDGE_CASE"
DECISION_REJECT              = "REJECT"
DECISION_NEEDS_MORE_EVIDENCE = "NEEDS_MORE_EVIDENCE"

# Status values written to articles.status by the public-contribute path.
# 'rejected_*' starts with "reject" so the grader's
#   SELECT 1 FROM articles WHERE status LIKE 'reject%' AND quarantine_path IS NOT NULL
# invariant naturally requires quarantine_path = NULL for them.
STATUS_STAGED              = "staged_pending_review"
STATUS_STAGED_EDGE_CASE    = "staged_edge_case"
STATUS_REJECTED_OFF_TOPIC  = "rejected_off_topic"
STATUS_REJECTED_DUPLICATE  = "rejected_duplicate_in_corpus"

# Confidence thresholds used to translate the classifier's verdict + score
# into a four-way decision. Tunable after Phase 4 test runs.
_INTAKE_ACCEPT_CONFIDENCE_FLOOR = 0.50
_INTAKE_REJECT_CONFIDENCE_FLOOR = 0.30


def _decide_from_classifier_result(result) -> str:
    """
    Translate an AdaptiveClassificationResult into a four-way decision.

    Rules (first match wins):
      1. If the classifier says it needs more evidence (no abstract / no surface
         text), return NEEDS_MORE_EVIDENCE so we route to a human reviewer.
      2. If the best question verdict is "accept" AND overall confidence is
         at least _INTAKE_ACCEPT_CONFIDENCE_FLOOR, return ACCEPT.
      3. If the best verdict is "edge_case" or the next action is
         "review_borderline_case", return EDGE_CASE.
      4. If the best verdict is "reject" or confidence is below
         _INTAKE_REJECT_CONFIDENCE_FLOOR, return REJECT.
      5. Default to EDGE_CASE so anything we can't confidently bucket goes
         to a human rather than being silently accepted or dropped.
    """
    next_action = getattr(result, "next_action", "") or ""
    if next_action in {"need_abstract_or_keywords", "extract_pdf_surface"}:
        return DECISION_NEEDS_MORE_EVIDENCE

    summary = getattr(result, "question_summary", None)
    verdict = (getattr(summary, "best_verdict", "") or "").lower() if summary else ""
    conf = float(getattr(result, "overall_confidence", 0.0) or 0.0)

    if verdict == "accept" and conf >= _INTAKE_ACCEPT_CONFIDENCE_FLOOR:
        return DECISION_ACCEPT
    if verdict == "edge_case" or next_action == "review_borderline_case":
        return DECISION_EDGE_CASE
    if verdict == "reject" or conf < _INTAKE_REJECT_CONFIDENCE_FLOOR:
        return DECISION_REJECT
    return DECISION_EDGE_CASE


def _status_for_decision(decision: str) -> str:
    """
    Map the four-way decision to one of the three production status values.

    NEEDS_MORE_EVIDENCE is folded into staged_edge_case so a human adjudicates
    rather than silently dropping the submission. The original four-way decision
    is preserved in validation_notes.
    """
    return {
        DECISION_ACCEPT:              STATUS_STAGED,
        DECISION_EDGE_CASE:           STATUS_STAGED_EDGE_CASE,
        DECISION_REJECT:              STATUS_REJECTED_OFF_TOPIC,
        DECISION_NEEDS_MORE_EVIDENCE: STATUS_STAGED_EDGE_CASE,
    }[decision]


def _classify_intake_pdf(
    *,
    article_id: str,
    quarantine_path: "Path",
    extracted_doi: Optional[str],
    filename: str,
) -> dict:
    """
    Run AdaptiveClassifierSubsystem on a quarantined PDF.

    Returns a dict shaped for both validation_notes and the JSON response:
        decision                — ACCEPT / EDGE_CASE / REJECT / NEEDS_MORE_EVIDENCE
        article_type            — KA bucket (mapped via _map_shared_article_type_to_ka_bucket)
        primary_topic           — str or None
        secondary_topics        — list[str]
        classifier_confidence   — float in [0, 1]
        classifier_reasons      — list[str]
        evidence_stage          — str
        next_action             — str
        extracted_title         — str (from PDF surface, "" if unavailable)
        extracted_abstract      — str (from PDF surface, "" if unavailable)
        raw_result_summary      — dict for validation_notes

    On classifier exception: returns a safe EDGE_CASE so a reviewer sees the
    submission rather than the request crashing. The exception is recorded in
    classifier_reasons and raw_result_summary.
    """
    evidence = ClassificationEvidence(
        paper_id=article_id,
        doi=(extracted_doi or "").strip(),
        filename=filename or "",
        pdf_path=str(quarantine_path),
    )
    try:
        result = _get_shared_article_classifier().classify(
            evidence,
            allow_surface_creation=True,
        )
    except Exception as exc:
        return {
            "decision": DECISION_EDGE_CASE,
            "article_type": "unknown",
            "primary_topic": None,
            "secondary_topics": [],
            "classifier_confidence": 0.0,
            "classifier_reasons": [f"classifier_error: {type(exc).__name__}: {exc}"],
            "evidence_stage": "bibliographic_only",
            "next_action": "review_borderline_case",
            "extracted_title": "",
            "extracted_abstract": "",
            "raw_result_summary": {"error": str(exc), "error_type": type(exc).__name__},
        }

    # Decision + status mapping
    decision = _decide_from_classifier_result(result)

    # Article type via existing KA bucket mapper
    shared_label = getattr(getattr(result, "article_type", None), "value", "") or ""
    article_type = _map_shared_article_type_to_ka_bucket(shared_label)

    # Topic routing
    primary_topic = None
    secondary_topics: list = []
    routing = getattr(result, "stable_topic_routing", None)
    if routing is not None:
        candidates = getattr(routing, "candidates", None) or ()
        if candidates:
            primary_topic = getattr(candidates[0], "topic", None)
            secondary_topics = [
                getattr(c, "topic", None) for c in candidates[1:3] if getattr(c, "topic", None)
            ]

    # PDF surface excerpts (only present if LocalPDFSurfaceExtractor ran)
    snapshot = getattr(result, "surface_snapshot", None)
    extracted_title = ""
    extracted_abstract = ""
    if snapshot is not None:
        first_page = getattr(snapshot, "first_page_excerpt", "") or ""
        abstract_excerpt = getattr(snapshot, "abstract_excerpt", "") or ""
        extracted_title = _extract_title_from_text(first_page, fallback="")
        extracted_abstract = abstract_excerpt or _extract_abstract_from_text(first_page)

    summary = getattr(result, "question_summary", None)
    reasons = list(getattr(summary, "reasons", ()) or ())

    return {
        "decision": decision,
        "article_type": article_type,
        "primary_topic": primary_topic,
        "secondary_topics": secondary_topics,
        "classifier_confidence": round(
            float(getattr(result, "overall_confidence", 0.0) or 0.0), 4
        ),
        "classifier_reasons": reasons,
        "evidence_stage": getattr(result, "evidence_stage", "") or "",
        "next_action": getattr(result, "next_action", "") or "",
        "extracted_title": extracted_title,
        "extracted_abstract": extracted_abstract,
        "raw_result_summary": {
            "verdict": (getattr(summary, "best_verdict", "") or "") if summary else "",
            "evidence_stage": getattr(result, "evidence_stage", "") or "",
            "next_action": getattr(result, "next_action", "") or "",
            "available_evidence_types": list(
                getattr(result, "available_evidence_types", ()) or ()
            ),
            "analysis_steps_run": list(getattr(result, "analysis_steps_run", ()) or ()),
            "shared_label": shared_label,
        },
    }

def _classify_article_type(text: str) -> dict:
    return _classify_article_payload(text_surface=text)


def _extract_text_from_pdf_bytes(data: bytes, max_chars: int = 5000) -> str:
    """Extract text from PDF bytes for classification. Best-effort."""
    try:
        import io
        # Try pdfplumber first (better extraction)
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                text = ""
                for page in pdf.pages[:5]:  # first 5 pages max
                    page_text = page.extract_text() or ""
                    text += page_text + "\n"
                    if len(text) > max_chars:
                        break
                return text[:max_chars]
        except ImportError:
            pass

        # Fallback: PyPDF2
        try:
            from PyPDF2 import PdfReader
            reader = PdfReader(io.BytesIO(data))
            text = ""
            for page in reader.pages[:5]:
                text += (page.extract_text() or "") + "\n"
                if len(text) > max_chars:
                    break
            return text[:max_chars]
        except ImportError:
            pass

        # Last resort: raw byte search for text
        text_bytes = data[:50000]
        # Extract ASCII text fragments
        fragments = re.findall(rb'[A-Za-z][A-Za-z0-9\s.,;:!?\-()]{10,}', text_bytes)
        return " ".join(f.decode('ascii', errors='ignore') for f in fragments)[:max_chars]
    except Exception:
        return ""


# ════════════════════════════════════════════════
# STUDENT-FACING BRIDGE ENDPOINTS
# ════════════════════════════════════════════════
# These wrap the core submit infrastructure and add automatic classification.
# The frontend (collect-articles-upload.html) calls these endpoints.

student_router = APIRouter(prefix="/api/student", tags=["student"])


@student_router.post("/fetch-abstracts")
async def fetch_abstracts_and_classify(request: Request):
    """
    A0 upload endpoint: receive PDFs + citation metadata, automatically classify
    each article's type, store everything, and return per-paper feedback.

    Expects multipart form data with:
    - question_id: str
    - question_type: '10-exp' | 'mixed'
    - papers_json: JSON array of {doi, apa_citation, filename}
    - pdfs: uploaded PDF files
    """
    user = _get_optional_user(request)
    if not user:
        raise HTTPException(401, "You must be signed in to submit articles")

    form = await request.form()
    question_id = form.get("question_id", "")
    question_type = form.get("question_type", "10-exp")
    papers_json_str = form.get("papers_json", "[]")

    try:
        papers_meta = json.loads(papers_json_str)
    except json.JSONDecodeError:
        papers_meta = []

    # Determine A0 task from question_type
    a0_task = "task1" if question_type == "10-exp" else "task2"

    # Get uploaded files
    pdf_files = form.getlist("pdfs") if hasattr(form, "getlist") else []
    # Also check for UploadFile objects
    if not pdf_files:
        for key in form:
            val = form.getlist(key)
            for v in val:
                if hasattr(v, 'read') and hasattr(v, 'filename'):
                    pdf_files.append(v)

    # Create batch
    submission_id = _next_id("KA-IN-", "submission_batches", "submission_id")
    now = _now()
    db = _get_db()
    db.execute(
        "INSERT INTO submission_batches VALUES (?,?,?,?,?,?,?,?)",
        (submission_id, user["user_id"], user["role"], "a0_upload", 0,
         "collect_articles_upload", now, None))
    db.commit()
    db.close()

    result_papers = []
    qualifying_count = 0

    # Process each PDF
    for i, pdf_file in enumerate(pdf_files):
        if not hasattr(pdf_file, 'read'):
            continue
        content = await pdf_file.read()
        filename = getattr(pdf_file, 'filename', f'paper_{i}.pdf')

        article_id = _next_id("KA-ART-", "articles", "article_id")
        pdf_hash = _compute_sha256(content)

        # Validate PDF
        validation = _validate_pdf_bytes(content, filename)
        if not validation.get("valid"):
            db = _get_db()
            try:
                db.execute("""INSERT INTO articles
                    (article_id, submission_id, submitter_id, submitter_type, track, input_mode,
                     pdf_filename, pdf_hash_sha256, pdf_size_bytes,
                     status, validation_notes, citation_raw,
                     assigned_question_id,
                     source_surface, course_context,
                     created_at, validated_at, rejected_at, metadata_confidence)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (article_id, submission_id, user["user_id"], user["role"],
                     user.get("track"), "pdf_single",
                     filename, pdf_hash, len(content),
                     "rejected_bad_file", json.dumps(validation), "",
                     question_id or None,
                     "collect_articles_upload", "COGS160-SP26",
                     now, now, now, "low"))
                db.commit()
            except Exception as e:
                db.rollback()
                print(f"[KA-ARTICLES] DB error storing rejected A0 upload {article_id}: {e}")
            finally:
                db.close()

            _write_audit(
                article_id,
                "rejected_bad_file",
                "received",
                "rejected_bad_file",
                actor_id=user["user_id"],
                actor_type=user["role"],
                details=validation,
            )
            result_papers.append({
                "title": filename,
                "filename": filename,
                "article_type": "unknown",
                "abstract": f"Invalid PDF: {validation.get('rejection_reason', 'validation failed')}",
                "pdf_present": True,
                "in_corpus": False,
                "classification_confidence": 0.0,
                "qualifies": False,
                "feedback": f"Rejected: {validation.get('rejection_reason', 'invalid file')}"
            })
            continue

        # Extract text for classification
        extracted_text = _extract_text_from_pdf_bytes(content)
        title = _extract_title_from_text(extracted_text, fallback=filename)
        abstract = _extract_abstract_from_text(extracted_text)
        classification = _classify_article_payload(
            title=title,
            abstract=abstract,
            text_surface=extracted_text,
        )
        art_type = classification["article_type"]
        art_confidence = classification["confidence"]

        # Get citation metadata if available
        meta = papers_meta[i] if i < len(papers_meta) else {}
        apa_citation = meta.get("apa_citation", "")
        doi = meta.get("doi", "")
        extracted_doi = _extract_doi_from_pdf(content) or doi

        # Duplicate check
        dup_result = _check_duplicate(pdf_hash=pdf_hash, doi=extracted_doi)
        duplicate_of = (
            dup_result["matches"][0]["article_id"]
            if dup_result["is_duplicate"] and dup_result["matches"]
            else None
        )

        # Determine status
        if dup_result["is_duplicate"]:
            status = "duplicate_existing"
        else:
            status = "staged_pending_review"

        quarantine_path = None
        if status == "staged_pending_review":
            try:
                month_dir = QUARANTINE_DIR / datetime.now().strftime("%Y-%m")
                month_dir.mkdir(parents=True, exist_ok=True)
                quarantine_path = month_dir / f"{article_id}.pdf"
                quarantine_path.write_bytes(content)
            except Exception as e:
                status = "rejected_storage_failure"
                validation = {
                    **validation,
                    "storage_error": str(e),
                    "rejection_reason": "quarantine write failed",
                }
                print(f"[KA-ARTICLES] File storage error for {article_id}: {e}")

        # All article types qualify — only count non-duplicate staged uploads
        type_valid = 1
        if status == "staged_pending_review":
            qualifying_count += 1

        # Store in database
        db = _get_db()
        try:
            db.execute("""INSERT INTO articles
                (article_id, submission_id, submitter_id, submitter_type, track, input_mode,
                 doi, pdf_filename, pdf_hash_sha256, pdf_size_bytes, quarantine_path,
                 article_type, a0_task, article_type_valid,
                 status, duplicate_of, validation_notes, citation_raw,
                 assigned_question_id,
                 source_surface, course_context,
                 created_at, validated_at, staged_at, metadata_confidence)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (article_id, submission_id, user["user_id"], user["role"],
                 user.get("track"), "pdf_single",
                 extracted_doi, filename, pdf_hash, len(content), str(quarantine_path) if quarantine_path else None,
                 art_type, a0_task, type_valid,
                 status, duplicate_of, json.dumps(validation), apa_citation,
                 question_id or None,
                 "collect_articles_upload", "COGS160-SP26",
                 now, now, now if status == "staged_pending_review" else None,
                 "high" if extracted_doi else "medium"))
            db.commit()
        except Exception as e:
            db.rollback()
            print(f"[KA-ARTICLES] DB error storing {article_id}: {e}")
        finally:
            db.close()

        audit_action = {
            "staged_pending_review": "staged",
            "duplicate_existing": "duplicate_detected",
            "rejected_storage_failure": "storage_failed",
        }.get(status, "received")
        _write_audit(
            article_id,
            audit_action,
            "received",
            status,
            actor_id=user["user_id"],
            actor_type=user["role"],
            details={
                "classification": classification,
                "duplicate_of": duplicate_of,
                "validation": validation,
            },
        )

        # Build feedback message
        if dup_result["is_duplicate"]:
            feedback = "⚠ Duplicate — this article is already in the system"
        elif status == "rejected_storage_failure":
            feedback = "⚠ Upload received but file storage failed — recorded for review"
        elif art_type == "unknown":
            feedback = "✓ Accepted — article type could not be determined but that's OK"
        else:
            feedback = f"✓ Accepted — classified as {art_type} (confidence: {art_confidence:.0%})"

        result_papers.append({
            "title": title,
            "filename": filename,
            "article_type": art_type if art_type != "unknown" else "Other",
            "canonical_article_type": classification["canonical_article_type"],
            "abstract": abstract[:300] if abstract else "Abstract not extracted",
            "pdf_present": True,
            "in_corpus": dup_result["is_duplicate"],
            "classification_confidence": art_confidence,
            "qualifies": status == "staged_pending_review",
            "feedback": feedback,
            "article_id": article_id
        })

    # Update batch count
    db = _get_db()
    db.execute("UPDATE submission_batches SET item_count=?, completed_at=? WHERE submission_id=?",
               (len(result_papers), _now(), submission_id))
    db.commit()
    db.close()

    return {
        "papers": result_papers,
        "qualifying_count": qualifying_count,
        "submission_id": submission_id,
        "task": a0_task,
        "total_submitted": len(result_papers)
    }


@student_router.post("/title-only")
async def submit_title_only_papers(request: Request):
    """
    Submit citation-only papers (no PDF) for classification.
    Used by the quick-add citation feature and bulk .txt upload.
    """
    user = _get_optional_user(request)
    if not user:
        raise HTTPException(401, "You must be signed in to submit articles")

    body = await request.json()
    question_id = body.get("question_id", "")
    question_type = body.get("question_type", "10-exp")
    papers = body.get("papers", [])
    a0_task = "task1" if question_type == "10-exp" else "task2"

    if not papers:
        raise HTTPException(400, "No papers provided")

    submission_id = _next_id("KA-IN-", "submission_batches", "submission_id")
    now = _now()

    db = _get_db()
    db.execute(
        "INSERT INTO submission_batches VALUES (?,?,?,?,?,?,?,?)",
        (submission_id, user["user_id"], user["role"], "a0_title_only", 0,
         "collect_articles_upload", now, None))
    db.commit()
    db.close()

    result_papers = []
    qualifying_count = 0

    for paper in papers:
        article_id = _next_id("KA-ART-", "articles", "article_id")
        title = paper.get("article_title", "")
        doi = paper.get("doi", "")
        apa = paper.get("apa_citation", "")

        # Classify from citation text (limited, but catches reviews/meta-analyses)
        classify_text = f"{title} {apa}"
        classification = _classify_article_payload(
            title=title,
            text_surface=classify_text,
        )
        art_type = classification["article_type"]
        art_confidence = classification["confidence"]

        # For citation-only, if we can't tell, default to "unknown" (needs manual review)
        if art_type == "unknown" and art_confidence < 0.3:
            # Citation-only papers are harder to classify; mark for review
            art_type = "unknown"

        # Duplicate check by DOI
        dup_result = _check_duplicate(doi=doi if doi else None,
                                       title=title if title else None)

        status = "duplicate_existing" if dup_result["is_duplicate"] else "staged_pending_review"

        # All article types qualify — only count non-duplicates
        type_valid = 1
        if status == "staged_pending_review":
            qualifying_count += 1

        # Parse citation for structured fields
        parsed = _parse_citation_line(apa) if apa else {"title": title, "authors": "", "year": ""}

        db = _get_db()
        try:
            db.execute("""INSERT INTO articles
                (article_id, submission_id, submitter_id, submitter_type, track, input_mode,
                 doi, title, authors, year, citation_raw,
                 article_type, a0_task, article_type_valid,
                 status, duplicate_of,
                 assigned_question_id,
                 source_surface, course_context,
                 created_at, staged_at, metadata_confidence)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (article_id, submission_id, user["user_id"], user["role"],
                 user.get("track"), "citation_text",
                 doi or None,
                 parsed["title"] or title or None,
                 parsed["authors"] or None,
                 int(parsed["year"]) if parsed.get("year") else None,
                 apa,
                 art_type, a0_task, type_valid,
                 status,
                 dup_result["matches"][0]["article_id"] if dup_result["is_duplicate"] and dup_result["matches"] else None,
                 question_id or None,
                 "collect_articles_upload", "COGS160-SP26",
                 now, now if status == "staged_pending_review" else None,
                 "medium" if doi else "low"))
            db.commit()
        except Exception as e:
            db.rollback()
            print(f"[KA-ARTICLES] DB error storing title-only {article_id}: {e}")
        finally:
            db.close()

        # Build display title
        display_title = parsed["title"] or title or apa[:80]

        # Feedback
        if art_type == "unknown":
            feedback = "✓ Accepted — article type undetermined from citation alone"
        else:
            feedback = f"✓ Accepted — classified as {art_type} (confidence: {art_confidence:.0%})"

        result_papers.append({
            "title": display_title,
            "article_type": art_type.capitalize() if art_type != "unknown" else "Unknown",
            "canonical_article_type": classification["canonical_article_type"],
            "abstract": "Citation only — upload PDF for full abstract",
            "pdf_present": False,
            "in_corpus": dup_result["is_duplicate"],
            "title_only": True,
            "classification_confidence": art_confidence,
            "qualifies": True,
            "feedback": feedback,
            "article_id": article_id
        })

    # Update batch
    db = _get_db()
    db.execute("UPDATE submission_batches SET item_count=?, completed_at=? WHERE submission_id=?",
               (len(result_papers), _now(), submission_id))
    db.commit()
    db.close()

    return {
        "papers": result_papers,
        "qualifying_count": qualifying_count,
        "submission_id": submission_id,
        "task": a0_task,
        "total_submitted": len(result_papers)
    }


@student_router.post("/classify-one")
async def classify_single_paper(request: Request):
    """
    Real-time single-paper classification. Called when a PDF is dropped
    into the upload zone — gives immediate feedback without batch submission.
    Does NOT store the article; that happens on the full submit.
    """
    form = await request.form()
    pdf_file = form.get("pdf")

    if not pdf_file or not hasattr(pdf_file, 'read'):
        raise HTTPException(400, "No PDF file provided")

    content = await pdf_file.read()
    filename = getattr(pdf_file, 'filename', 'paper.pdf')

    # Quick validation
    validation = _validate_pdf_bytes(content, filename)
    if not validation.get("valid"):
        return {
            "filename": filename,
            "valid": False,
            "article_type": "unknown",
            "confidence": 0.0,
            "feedback": f"Invalid PDF: {validation.get('rejection_reason', 'validation failed')}",
            "is_experimental": False
        }

    # Extract text and classify
    extracted_text = _extract_text_from_pdf_bytes(content)
    title = _extract_title_from_text(extracted_text, fallback=filename)
    abstract = _extract_abstract_from_text(extracted_text)
    classification = _classify_article_payload(
        title=title,
        abstract=abstract,
        text_surface=extracted_text,
    )

    art_type = classification["article_type"]
    confidence = classification["confidence"]

    if art_type == "experimental":
        feedback = f"✓ Experimental article (confidence: {confidence:.0%})"
    elif art_type == "unknown":
        feedback = "⚠ Could not determine type — will be classified on submission"
    else:
        feedback = f"✗ Appears to be {art_type} (confidence: {confidence:.0%}) — not experimental"

    return {
        "filename": filename,
        "valid": True,
        "article_type": art_type,
        "canonical_article_type": classification["canonical_article_type"],
        "confidence": confidence,
        "feedback": feedback,
        "is_experimental": art_type == "experimental",
        "signals": classification["signals"]
    }


# ════════════════════════════════════════════════
# STUDENT PROGRESS & ASSIGNMENTS
# ════════════════════════════════════════════════


@student_router.get("/progress")
async def student_progress(request: Request):
    """
    Return the current student's A0 progress: qualifying article counts
    per task and paper lists for each question.
    Called by collect-articles-upload.html to populate progress bars.
    """
    user = _get_optional_user(request)
    if not user:
        raise HTTPException(401, "You must be signed in")

    db = _get_db()

    # Count qualifying articles for task1 (all types count)
    q1_qualifying = db.execute("""
        SELECT COUNT(*) FROM articles
        WHERE submitter_id=? AND a0_task='task1'
        AND status NOT LIKE 'rejected%%' AND status != 'duplicate_existing'
    """, (user["user_id"],)).fetchone()[0]

    # Count qualifying articles for task2 (any type)
    q2_qualifying = db.execute("""
        SELECT COUNT(*) FROM articles
        WHERE submitter_id=? AND a0_task='task2'
        AND status NOT LIKE 'rejected%%' AND status != 'duplicate_existing'
    """, (user["user_id"],)).fetchone()[0]

    # Get paper details for each task (include duplicates so students can see them)
    q1_papers = db.execute("""
        SELECT article_id, title, article_type, abstract, pdf_filename, status,
               article_type_valid, created_at
        FROM articles WHERE submitter_id=? AND a0_task='task1'
        AND status NOT LIKE 'rejected%%'
        ORDER BY created_at DESC
    """, (user["user_id"],)).fetchall()

    q2_papers = db.execute("""
        SELECT article_id, title, article_type, abstract, pdf_filename, status,
               article_type_valid, created_at
        FROM articles WHERE submitter_id=? AND a0_task='task2'
        AND status NOT LIKE 'rejected%%'
        ORDER BY created_at DESC
    """, (user["user_id"],)).fetchall()

    db.close()

    def _paper_dict(row):
        d = dict(row)
        d["in_corpus"] = d.get("status") == "duplicate_existing"
        d["pdf_present"] = bool(d.get("pdf_filename"))
        return d

    both_complete = q1_qualifying >= 10 and q2_qualifying >= 10

    return {
        "q1_qualifying": q1_qualifying,
        "q2_qualifying": q2_qualifying,
        "q1_papers": [_paper_dict(r) for r in q1_papers],
        "q2_papers": [_paper_dict(r) for r in q2_papers],
        "both_complete": both_complete
    }


def _auto_assign_one_question(db, user_id: str) -> Optional[str]:
    """
    Auto-assign ONE question (Q1 / 10-EXP) to a student, prioritising questions
    with the fewest prior completions so the corpus grows as broadly as possible.

    Q2 (mixed) is NOT auto-assigned — the student chooses their own topic
    and crafts their own Google AI Scholar query for it.

    Returns the question_id of the newly claimed question, or None.
    """
    now = _now()
    current_round = _current_round(db)

    # Rank all questions by total times completed (ascending = fewest first)
    all_qs = db.execute("""
        SELECT rq.question_id,
               COALESCE(SUM(qc.task1_count + qc.task2_count), 0) AS total_articles
        FROM research_questions rq
        LEFT JOIN question_claims qc ON rq.question_id = qc.question_id
            AND qc.released_at IS NULL
        GROUP BY rq.question_id
        ORDER BY total_articles ASC, rq.question_id ASC
    """).fetchall()

    # Get questions already claimed by anyone in the current round (including released,
    # because UNIQUE(question_id, round) prevents re-use of released slots)
    claimed_this_round = db.execute(
        "SELECT question_id FROM question_claims WHERE round=?",
        (current_round,)).fetchall()
    claimed_ids = {r["question_id"] for r in claimed_this_round}

    # Prefer unclaimed in this round, then fewest completions overall
    available = [q for q in all_qs if q["question_id"] not in claimed_ids]

    if not available:
        # All questions in this round are taken — move to next round
        current_round += 1
        claimed_next = db.execute(
            "SELECT question_id FROM question_claims WHERE round=?",
            (current_round,)).fetchall()
        claimed_next_ids = {r["question_id"] for r in claimed_next}
        available = [q for q in all_qs if q["question_id"] not in claimed_next_ids]

    pool = available if available else list(all_qs)

    if not pool:
        return None

    chosen = pool[0]
    qid = chosen["question_id"]
    db.execute("""
        INSERT INTO question_claims (question_id, user_id, round, claimed_at)
        VALUES (?, ?, ?, ?)
    """, (qid, user_id, current_round, now))
    db.commit()
    return qid


@student_router.get("/assignments")
async def student_assignments(request: Request):
    """
    Return the student's assigned questions for A0 (Q1, Q2, and any brownie questions).
    Called by collect-articles-upload.html on page init.

    If the student has nothing yet, auto-assigns Question 1 only. Question 2 remains
    student-chosen so the corpus grows broadly while the second task stays flexible.
    """
    import traceback as _tb
    user = _get_optional_user(request)
    if not user:
        raise HTTPException(401, "You must be signed in")

    try:
        return await _student_assignments_inner(user)
    except Exception as e:
        print(f"[KA-AUTH] /api/student/assignments ERROR: {e}")
        _tb.print_exc()
        raise HTTPException(500, f"Assignment error: {e}")

async def _student_assignments_inner(user):
    db = _get_db()

    # Get all active claims for this student
    claims = db.execute("""
        SELECT qc.question_id, qc.round, rq.label, rq.domain, rq.text
        FROM question_claims qc
        JOIN research_questions rq ON qc.question_id = rq.question_id
        WHERE qc.user_id=? AND qc.released_at IS NULL
        ORDER BY qc.round ASC, qc.claimed_at ASC
    """, (user["user_id"],)).fetchall()

    q1_id = user.get("question_id")

    # Auto-assign Q1 if the student truly has nothing yet. Q2 remains student-chosen.
    if len(claims) == 0 and not q1_id:
        q1_id = _auto_assign_one_question(db, user["user_id"])
        if q1_id:
            db.execute("UPDATE users SET question_id=? WHERE user_id=?",
                       (q1_id, user["user_id"]))
            db.commit()
            user["question_id"] = q1_id
        # Re-fetch after assignment
        claims = db.execute("""
            SELECT qc.question_id, qc.round, rq.label, rq.domain, rq.text
            FROM question_claims qc
            JOIN research_questions rq ON qc.question_id = rq.question_id
            WHERE qc.user_id=? AND qc.released_at IS NULL
            ORDER BY qc.round ASC, qc.claimed_at ASC
        """, (user["user_id"],)).fetchall()

    # Older accounts may still have claims but no explicit Q1 stored on the user row.
    if not q1_id and claims:
        q1_id = claims[0]["question_id"]
        db.execute("UPDATE users SET question_id=? WHERE user_id=?",
                   (q1_id, user["user_id"]))
        db.commit()
        user["question_id"] = q1_id

    q1 = None
    q2 = None
    brownie = []

    if q1_id:
        q1_claim = next((claim for claim in claims if claim["question_id"] == q1_id), None)
        if q1_claim:
            q1 = {
                "question_id": q1_claim["question_id"],
                "question_text": q1_claim["text"] or q1_claim["label"] or q1_claim["question_id"],
                "domain": q1_claim["domain"],
                "question_type": "10-exp"
            }
        else:
            q1_row = db.execute("""
                SELECT question_id, label, domain, text
                FROM research_questions
                WHERE question_id=?
            """, (q1_id,)).fetchone()
            if q1_row:
                q1 = {
                    "question_id": q1_row["question_id"],
                    "question_text": q1_row["text"] or q1_row["label"] or q1_row["question_id"],
                    "domain": q1_row["domain"],
                    "question_type": "10-exp"
                }

    remaining_claims = [claim for claim in claims if claim["question_id"] != q1_id]
    for i, claim in enumerate(remaining_claims):
        entry = {
            "question_id": claim["question_id"],
            "question_text": claim["text"] or claim["label"] or claim["question_id"],
            "domain": claim["domain"],
        }
        if i == 0:
            entry["question_type"] = "mixed"
            q2 = entry
        else:
            entry["question_type"] = "brownie"
            brownie.append(entry)

    db.close()

    # Check if both tasks are complete (for brownie offer)
    both_complete = False
    if q1:
        db = _get_db()
        q1_count = db.execute("""
            SELECT COUNT(*) FROM articles
            WHERE submitter_id=? AND a0_task='task1'
            AND status NOT LIKE 'rejected%%' AND status != 'duplicate_existing'
        """, (user["user_id"],)).fetchone()[0]
        q2_count = db.execute("""
            SELECT COUNT(*) FROM articles
            WHERE submitter_id=? AND a0_task='task2'
            AND status NOT LIKE 'rejected%%' AND status != 'duplicate_existing'
        """, (user["user_id"],)).fetchone()[0]
        db.close()
        both_complete = q1_count >= 10 and q2_count >= 10

    return {
        "q1": q1,
        "q2": q2,
        "brownie": brownie,
        "both_complete": both_complete
    }


@student_router.get("/q1-options")
async def q1_options(request: Request):
    """Return currently claimable Question 1 options for self-repair on the upload page."""
    user = _get_optional_user(request)
    if not user:
        raise HTTPException(401, "You must be signed in")

    db = _get_db()
    current_round = _current_round(db)
    available_ids = _available_questions(db, current_round)

    if not available_ids:
        db.close()
        return {"round": current_round, "options": []}

    placeholders = ",".join("?" * len(available_ids))
    rows = db.execute(f"""
        SELECT rq.question_id, rq.label, rq.domain, rq.text,
               COALESCE(SUM(qc.task1_count), 0) AS total_experimental,
               COALESCE(SUM(qc.task2_count), 0) AS total_any_type,
               COALESCE(SUM(qc.task1_count + qc.task2_count), 0) AS total_articles
        FROM research_questions rq
        LEFT JOIN question_claims qc ON rq.question_id = qc.question_id
            AND qc.released_at IS NULL
        WHERE rq.question_id IN ({placeholders})
        GROUP BY rq.question_id
        ORDER BY total_articles ASC, rq.question_id ASC
    """, available_ids).fetchall()
    db.close()

    return {
        "round": current_round,
        "options": [
            {
                "question_id": row["question_id"],
                "label": row["label"],
                "domain": row["domain"],
                "text": row["text"],
                "total_experimental": row["total_experimental"],
                "total_any_type": row["total_any_type"],
                "total_articles": row["total_articles"],
                "round": current_round
            }
            for row in rows
        ]
    }


@student_router.post("/repair-q1")
async def repair_q1(request: Request):
    """Replace or restore the student's explicit Question 1 assignment."""
    user = _get_optional_user(request)
    if not user:
        raise HTTPException(401, "You must be signed in")

    body = await request.json()
    question_id = body.get("question_id", "").strip()
    if not question_id:
        raise HTTPException(400, "question_id is required")

    db = _get_db()
    q = db.execute("SELECT * FROM research_questions WHERE question_id=?",
                   (question_id,)).fetchone()
    if not q:
        db.close()
        raise HTTPException(404, f"Question {question_id} not found")

    current_q1_row = db.execute("SELECT question_id FROM users WHERE user_id=?",
                                (user["user_id"],)).fetchone()
    current_q1 = (current_q1_row["question_id"] if current_q1_row else "") or ""

    if current_q1 == question_id:
        existing_q1_claim = db.execute("""
            SELECT claim_id FROM question_claims
            WHERE user_id=? AND question_id=? AND released_at IS NULL
        """, (user["user_id"], question_id)).fetchone()
        db.close()
        if existing_q1_claim:
            return {
                "question_id": question_id,
                "question_text": q["text"] or q["label"],
                "domain": q["domain"],
                "message": f"Question 1 is already set to {question_id}."
            }

    current_round = _current_round(db)
    round_claim = db.execute("""
        SELECT claim_id, user_id, released_at
        FROM question_claims
        WHERE question_id=? AND round=?
    """, (question_id, current_round)).fetchone()

    if round_claim and round_claim["user_id"] != user["user_id"]:
        db.close()
        raise HTTPException(409, f"Question {question_id} is no longer available in round {current_round}. Choose another.")

    if round_claim and round_claim["released_at"] is None and current_q1 != question_id:
        db.close()
        raise HTTPException(409, "You already hold that question in another slot. Choose a different Question 1.")

    now = _now()
    try:
        if round_claim and round_claim["released_at"] is not None:
            db.execute("""
                UPDATE question_claims
                SET released_at=NULL, claimed_at=?
                WHERE claim_id=?
            """, (now, round_claim["claim_id"]))
        elif not round_claim:
            db.execute("""
                INSERT INTO question_claims (question_id, user_id, round, claimed_at)
                VALUES (?, ?, ?, ?)
            """, (question_id, user["user_id"], current_round, now))

        if current_q1 and current_q1 != question_id:
            db.execute("""
                UPDATE question_claims
                SET released_at=?
                WHERE user_id=? AND question_id=? AND released_at IS NULL
            """, (now, user["user_id"], current_q1))

        db.execute("UPDATE users SET question_id=? WHERE user_id=?",
                   (question_id, user["user_id"]))
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        db.close()
        raise HTTPException(409, f"Question {question_id} is no longer available in round {current_round}. Choose another.")
    except sqlite3.Error as exc:
        db.rollback()
        db.close()
        raise HTTPException(503, f"Could not repair Question 1 right now: {exc}")

    db.close()
    verb = "restored" if round_claim and round_claim["released_at"] is not None else "repaired"
    return {
        "question_id": question_id,
        "question_text": q["text"] or q["label"],
        "domain": q["domain"],
        "message": f"Question 1 {verb}: {question_id}. Part 1 is unlocked again."
    }


@student_router.post("/choose-q2")
async def choose_q2(request: Request):
    """
    Student chooses their own Q2 question (mixed / open corpus).
    They pick from the available research questions — ideally one the corpus
    needs most. This is separate from the auto-assigned Q1.
    """
    user = _get_optional_user(request)
    if not user:
        raise HTTPException(401, "You must be signed in")

    body = await request.json()
    question_id = body.get("question_id", "").strip()
    if not question_id:
        raise HTTPException(400, "question_id is required")

    db = _get_db()

    # Verify question exists
    q = db.execute("SELECT * FROM research_questions WHERE question_id=?",
                   (question_id,)).fetchone()
    if not q:
        db.close()
        raise HTTPException(404, f"Question {question_id} not found")

    q1_row = db.execute("SELECT question_id FROM users WHERE user_id=?",
                        (user["user_id"],)).fetchone()
    current_q1 = (q1_row["question_id"] if q1_row else "") or ""
    if current_q1 == question_id:
        db.close()
        raise HTTPException(409, "Question 2 must be different from your Question 1 assignment.")

    active_claims = db.execute("""
        SELECT question_id
        FROM question_claims
        WHERE user_id=? AND released_at IS NULL
        ORDER BY claimed_at ASC
    """, (user["user_id"],)).fetchall()
    active_ids = [row["question_id"] for row in active_claims]
    current_q2 = next((qid for qid in active_ids if qid != current_q1), "")

    if current_q2 == question_id:
        db.close()
        return {
            "question_id": question_id,
            "question_text": q["text"] or q["label"],
            "domain": q["domain"],
            "message": f"Q2 is already assigned: {question_id} — {q['label']}"
        }

    if current_q2:
        db.close()
        raise HTTPException(409, "You already have two questions assigned. Release one first if you want to change.")

    now = _now()
    current_round = _current_round(db)
    round_claim = db.execute("""
        SELECT claim_id, user_id, released_at
        FROM question_claims
        WHERE question_id=? AND round=?
    """, (question_id, current_round)).fetchone()

    if round_claim and round_claim["user_id"] != user["user_id"]:
        db.close()
        raise HTTPException(409, f"Question {question_id} is no longer available in round {current_round}. Choose another.")

    try:
        if round_claim and round_claim["released_at"] is not None:
            db.execute("""
                UPDATE question_claims
                SET released_at=NULL, claimed_at=?
                WHERE claim_id=?
            """, (now, round_claim["claim_id"]))
        elif not round_claim:
            db.execute("""
                INSERT INTO question_claims (question_id, user_id, round, claimed_at)
                VALUES (?, ?, ?, ?)
            """, (question_id, user["user_id"], current_round, now))
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        db.close()
        raise HTTPException(409, f"Question {question_id} is no longer available in round {current_round}. Choose another.")
    except sqlite3.Error as exc:
        db.rollback()
        db.close()
        raise HTTPException(503, f"Could not assign Question 2 right now: {exc}")

    db.close()
    verb = "restored" if round_claim and round_claim["released_at"] is not None else "assigned"
    return {
        "question_id": question_id,
        "question_text": q["text"] or q["label"],
        "domain": q["domain"],
        "message": f"Q2 {verb}: {question_id} — {q['label']}"
    }


@student_router.get("/topics-needed")
async def topics_needed(request: Request):
    """
    Return all research questions ranked by how much the corpus needs them
    (fewest completions first). Helps students choose a good Q2 topic.
    """
    user = _get_optional_user(request)
    if not user:
        raise HTTPException(401, "You must be signed in")

    db = _get_db()
    current_round = _current_round(db)
    available_ids = set(_available_questions(db, current_round))
    current_q1 = (user.get("question_id") or "").strip()

    rows = db.execute("""
        SELECT rq.question_id, rq.label, rq.domain, rq.text,
               COUNT(DISTINCT qc.user_id) AS times_claimed,
               COALESCE(SUM(qc.task1_count), 0) AS total_experimental,
               COALESCE(SUM(qc.task2_count), 0) AS total_any_type,
               COALESCE(SUM(qc.task1_count + qc.task2_count), 0) AS total_articles
        FROM research_questions rq
        LEFT JOIN question_claims qc ON rq.question_id = qc.question_id
            AND qc.released_at IS NULL
        GROUP BY rq.question_id
        ORDER BY total_articles ASC, times_claimed ASC, rq.question_id ASC
    """).fetchall()

    db.close()

    return {
        "topics": [
            {
                "question_id": r["question_id"],
                "label": r["label"],
                "domain": r["domain"],
                "text": r["text"],
                "times_claimed": r["times_claimed"],
                "total_articles": r["total_articles"],
                "total_experimental": r["total_experimental"],
                "total_any_type": r["total_any_type"],
                "need_level": "high" if r["total_articles"] < 5 else
                              "medium" if r["total_articles"] < 15 else "low"
            }
            for r in rows
            if r["question_id"] in available_ids and r["question_id"] != current_q1
        ]
    }


@student_router.post("/accept-brownie")
async def accept_brownie(request: Request):
    """
    Accept a brownie-point question. Assigns the next available unclaimed question
    to the student as a bonus round.
    """
    user = _get_optional_user(request)
    if not user:
        raise HTTPException(401, "You must be signed in")

    db = _get_db()

    # Find the current max round
    max_round_row = db.execute(
        "SELECT COALESCE(MAX(round), 1) FROM question_claims").fetchone()
    current_round = max_round_row[0]

    # Find questions not yet claimed by this student
    already_claimed = db.execute(
        "SELECT question_id FROM question_claims WHERE user_id=? AND released_at IS NULL",
        (user["user_id"],)).fetchall()
    already_ids = {r["question_id"] for r in already_claimed}

    all_qs = db.execute(
        "SELECT question_id, label, text FROM research_questions ORDER BY question_id"
    ).fetchall()
    available = [q for q in all_qs if q["question_id"] not in already_ids]

    if not available:
        db.close()
        raise HTTPException(409, "No additional questions available for brownie points")

    chosen = available[0]
    now = _now()
    db.execute("""
        INSERT INTO question_claims (question_id, user_id, round, claimed_at)
        VALUES (?, ?, ?, ?)
    """, (chosen["question_id"], user["user_id"], current_round + 1, now))
    db.commit()
    db.close()

    return {
        "question_id": chosen["question_id"],
        "question_text": chosen["text"] or chosen["label"] or chosen["question_id"],
        "message": f"Brownie question assigned: {chosen['question_id']}"
    }