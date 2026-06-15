#!/usr/bin/env python3
"""
build_bc_curriculum_db.py
=========================

Downloads the BC K-9 "Area of Learning" curriculum elaboration PDFs from
curriculum.gov.bc.ca and parses them into a structured database for the
IB PYP Year Planner.

For each subject and grade (K-7 by default) it extracts:
  - Big Ideas (+ their elaborations)
  - Curricular Competencies (+ matched elaborations, grouped by sub-heading)
  - Content standards (+ matched elaborations)

Outputs:
  out/bc_curriculum.json        single combined database (load this into the app)
  out/subjects/<slug>.json      one file per subject
  out/bc_curriculum.sqlite      normalised SQLite database (optional)
  out/raw/<slug>.txt            naive full-text extract (for inspection)
  out/raw/<slug>.columns.txt    column-separated text per grade (for tuning)
  out/pdfs/<slug>.pdf           the downloaded source PDFs (cached; re-runs skip download)

Definitions used:
  * One curricular competency / one content item = a single top-level bullet (•).
    Any nested sub-points beneath it (— dance: ..., — drama: ...) are folded into
    that same item. So in Content, "elements in the arts, including but not limited
    to:" plus its dance/drama/music/visual-arts sub-bullets is ONE item, and the
    next top-level bullet is a separate item.
  * The Learning Standards are a two-column table (Curricular Competencies | Content).
    They are separated by word x-position, then each column is read top-to-bottom,
    rather than relying on the PDF's (interleaving) reading order.

Usage:
  pip install requests pdfplumber
  python build_bc_curriculum_db.py
  python build_bc_curriculum_db.py --max-grade 7 --no-sqlite
  python build_bc_curriculum_db.py --subjects mathematics science
  python build_bc_curriculum_db.py --layout      # if column parsing looks wrong, try this

Notes:
  * Grade "K" = Kindergarten. --max-grade 7 keeps K through Grade 7.
  * Core French only exists from Grade 5 up, so it contributes grades 5-7.
  * Each subject lists several candidate URLs; the first that returns a valid
    PDF is used, so the script keeps working if the ministry renames a file.
  * The raw text dumps are written so you can eyeball exactly what pdfplumber
    saw if any subject/grade parses thin.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import sqlite3
import sys
import time
from datetime import date

try:
    import requests
except ImportError:
    sys.exit("Missing dependency. Run:  pip install requests pdfplumber")

try:
    import pdfplumber
except ImportError:
    sys.exit("Missing dependency. Run:  pip install requests pdfplumber")


# --------------------------------------------------------------------------- #
# Subject configuration
# --------------------------------------------------------------------------- #
# Each subject maps to:
#   header   : the ALL-CAPS "Area of Learning" name as printed in the PDF
#   urls     : candidate download URLs, tried in order until one works
#
# The base path uses the single-slash canonical form. The ministry CMS also
# serves a double-slash ("//files") variant; both resolve, single is cleaner.
BASE = "https://curriculum.gov.bc.ca/sites/curriculum.gov.bc.ca/files/curriculum"


def _candidates(slug: str, *ranges: str) -> list[str]:
    return [f"{BASE}/{slug}/en_{slug}_{r}_elab.pdf" for r in ranges]


SUBJECTS: dict[str, dict] = {
    "English Language Arts": {
        "slug": "english-language-arts",
        "header": "ENGLISH LANGUAGE ARTS",
        "urls": _candidates("english-language-arts", "k-9", "k-12"),
    },
    "Mathematics": {
        "slug": "mathematics",
        "header": "MATHEMATICS",
        "urls": _candidates("mathematics", "k-9"),
    },
    "Science": {
        "slug": "science",
        "header": "SCIENCE",
        "urls": _candidates("science", "k-9"),
    },
    "Social Studies": {
        "slug": "social-studies",
        "header": "SOCIAL STUDIES",
        "urls": _candidates("social-studies", "k-9"),
    },
    "Arts Education": {
        "slug": "arts-education",
        "header": "ARTS EDUCATION",
        "urls": _candidates("arts-education", "k-9"),
    },
    "Applied Design, Skills, and Technologies": {
        "slug": "adst",
        "header": "APPLIED DESIGN, SKILLS, AND TECHNOLOGIES",
        "urls": _candidates("adst", "k-9", "6-9"),
    },
    "Physical and Health Education": {
        "slug": "physical-health-education",
        "header": "PHYSICAL AND HEALTH EDUCATION",
        "urls": _candidates("physical-health-education", "k-9", "k-10"),
    },
    "Core French": {
        "slug": "core-french",
        "header": "CORE FRENCH",
        # Core French begins in Grade 5; the 5-12 file carries grades 5-7.
        "urls": _candidates("core-french", "5-12", "5-9")
        + [f"{BASE}/languages/en_languages_5-10_core-french_elab.pdf"],
    },
    "Career Education": {
        "slug": "career-education",
        "header": "CAREER EDUCATION",
        "urls": _candidates("career-education", "k-9"),
    },
}

HEADERS = {"User-Agent": "Mozilla/5.0 (BC-curriculum-db-builder)"}

# Character classes seen in the PDFs.
BULLETS = "\u2022\u00b7\u25aa\u25cf\u2023\u2043"      # • · ▪ ● ‣ ⁃
SUBBULLETS = "\u2014\u2013\u2010\u2011-"             # — – ‐ ‑ -
DASH = "[\u2013\u2014\u2010\u2011-]"                  # for "Big Ideas – Elaborations"

GRADE_WORDS = {
    "kindergarten": "K",
    **{f"grade {n}": str(n) for n in range(1, 13)},
}

# A grade label can be a single grade ("Grade 3", "Kindergarten") or a band
# ("Kindergarten–Grade 3", "Grades 4–5", "Grades 6–7"). Some areas of learning
# (notably ADST) define standards once per band rather than per grade.
GRADE_LABEL = (
    r"(?:Kindergarten(?:\s*[\u2013\u2014-]\s*Grade\s*\d+)?"
    r"|Grades?\s*\d+(?:\s*[\u2013\u2014-]\s*\d+)?)"
)


def parse_grade_label(label: str) -> list[str]:
    """Expand a grade label into the list of grade codes it covers."""
    low = re.sub(r"[\u2013\u2014]", "-", label.lower())
    has_k = "kindergarten" in low
    nums = [int(n) for n in re.findall(r"\d+", low)]
    is_range = "-" in low or "grades" in low or (has_k and nums)
    if is_range:
        start = 0 if has_k else (nums[0] if nums else 0)
        end = nums[-1] if nums else start
    elif has_k:
        start = end = 0
    elif nums:
        start = end = nums[0]
    else:
        return []
    return ["K" if c == 0 else str(c) for c in range(start, end + 1)]


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclasses.dataclass
class Standard:
    text: str
    group: str | None = None
    elaborations: dict[str, str] = dataclasses.field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #
def download(subject: str, cfg: dict, pdf_dir: str, force: bool) -> tuple[str, str | None] | None:
    """Download the first working candidate URL; return (path, url) or None."""
    dest = os.path.join(pdf_dir, f"{cfg['slug']}.pdf")
    if os.path.exists(dest) and not force and os.path.getsize(dest) > 1000:
        print(f"  [cache] {cfg['slug']}.pdf")
        return dest, None

    for url in cfg["urls"]:
        try:
            r = requests.get(url, headers=HEADERS, timeout=60)
        except requests.RequestException as exc:
            print(f"  [warn]  {url} -> {exc}")
            continue
        if r.status_code == 200 and r.content[:5] == b"%PDF-":
            with open(dest, "wb") as fh:
                fh.write(r.content)
            print(f"  [ok]    {cfg['slug']}.pdf  ({len(r.content)//1024} KB)  <- {url}")
            return dest, url
        print(f"  [skip]  HTTP {r.status_code} / not-a-pdf  <- {url}")
    print(f"  [FAIL]  could not download {subject}")
    return None


# --------------------------------------------------------------------------- #
# Text extraction
# --------------------------------------------------------------------------- #
def extract_text(pdf_path: str, layout: bool) -> str:
    """Extract all text from a PDF, page by page."""
    parts: list[str] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            txt = page.extract_text(layout=layout, x_tolerance=1.5, y_tolerance=3) or ""
            parts.append(txt)
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #
NOISE = re.compile(
    r"(curriculum\.gov\.bc\.ca"
    r"|Province of British Columbia"
    r"|Ministry of Education"
    r"|^\s*Page\s+\d+\s*$"
    r"|^\s*\d+\s*$)",
    re.IGNORECASE,
)


def normalise(text: str) -> str:
    """Normalise line endings and drop recurring page header/footer noise."""
    text = text.replace("\r", "\n")
    kept = [ln for ln in text.split("\n") if not NOISE.search(ln)]
    text = "\n".join(kept)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return text


def is_header_line(s: str) -> bool:
    """True for an ALL-CAPS subject banner line, e.g. 'ARTS EDUCATION'."""
    s = s.strip()
    return bool(s) and bool(re.fullmatch(r"[A-Z][A-Z &/,'.\-]{1,}", s))


def looks_like_group_header(line: str) -> bool:
    """Heuristic: a sub-heading like 'Reasoning and reflecting' or
    'Create and communicate (writing, speaking, representing)' or a content
    module name like 'Robotics'."""
    s = line.strip()
    if not s or s[0] in BULLETS:
        return False
    # A parenthetical clarifier is allowed; judge length on the part before it.
    core = s.split("(")[0].strip()
    if len(s) > 70 or len(core.split()) > 7:
        return False
    if s[-1] in ".;:,":          # trailing ) is fine (e.g. "(reading, ...)")
        return False
    if not s[0].isupper():
        return False
    # Reject obvious sentence continuations.
    if s.lower().startswith(("e.g", "i.e", "and ", "or ", "the ", "to ", "of ", "with ")):
        return False
    return True


def parse_standard_block(block: str, with_groups: bool) -> list[Standard]:
    """Turn a bulleted region of text into a list of Standard objects."""
    items: list[Standard] = []
    cur: Standard | None = None
    group: str | None = None

    for raw in block.splitlines():
        line = raw.rstrip()
        s = line.strip()
        if not s or is_header_line(s):
            continue
        if re.match(r"Students (?:will be able to|are expected to)", s, re.IGNORECASE):
            continue
        first = s[0]

        if first in BULLETS:
            if cur:
                items.append(cur)
            cur = Standard(text=s.lstrip(BULLETS).strip(), group=group)
        elif first in SUBBULLETS and cur is not None and len(s) > 1:
            # Sub-bullet (e.g. "— dance: body, space..."): fold into parent.
            cur.text += " " + s.lstrip(SUBBULLETS).strip()
        elif with_groups and cur is None and looks_like_group_header(s):
            group = s
        elif with_groups and cur is not None and looks_like_group_header(s) \
                and not cur.text.rstrip().endswith((",", "and", "or", "the", "of", ";", ":")):
            # A new heading after a completed bullet -> start a new group.
            items.append(cur)
            cur = None
            group = s
        else:
            # Wrapped continuation of the current bullet.
            if cur is not None:
                cur.text += " " + s
    if cur:
        items.append(cur)

    # Tidy whitespace.
    for it in items:
        it.text = re.sub(r"\s+", " ", it.text).strip(" .;")
    return [it for it in items if it.text]


def parse_elaborations(block: str) -> dict[str, str]:
    """Parse 'term: definition' elaboration bullets into a dict."""
    elabs: dict[str, str] = {}
    cur_term: str | None = None
    buf: list[str] = []

    def flush():
        nonlocal cur_term, buf
        if cur_term:
            elabs[cur_term] = re.sub(r"\s+", " ", " ".join(buf)).strip()
        cur_term, buf = None, []

    for raw in block.splitlines():
        s = raw.strip()
        if not s or is_header_line(s):
            continue
        if s[0] in BULLETS:
            flush()
            body = s.lstrip(BULLETS).strip()
            if ":" in body:
                term, _, definition = body.partition(":")
                cur_term = term.strip().lower()
                buf = [definition.strip()]
            else:
                cur_term = body.strip().lower()
                buf = [""]
        else:
            buf.append(s)
    flush()
    return {k: v for k, v in elabs.items() if k}


def attach_elaborations(standards: list[Standard], elabs: dict[str, str]) -> None:
    """Attach an elaboration to a standard when its key term (or any of its
    slash-separated variants, e.g. 'story/stories') appears in the text as a
    whole word."""
    compiled = []
    for term, definition in elabs.items():
        pats = []
        for variant in term.split("/"):
            v = variant.strip()
            if len(v) >= 3:
                pats.append(re.compile(r"\b" + re.escape(v) + r"\b", re.IGNORECASE))
        if pats:
            compiled.append((term, definition, pats))
    for st in standards:
        for term, definition, pats in compiled:
            if any(p.search(st.text) for p in pats):
                st.elaborations[term] = definition


# --------------------------------------------------------------------------- #
# Per-grade parsing
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Geometry-aware extraction
# --------------------------------------------------------------------------- #
# The "Learning Standards" block is a two-column table: Curricular Competencies
# on the left, Content on the right. pdfplumber's default text extraction reads
# line-by-line ACROSS both columns and interleaves them, so we instead split the
# words by their x-position and read each column top-to-bottom on its own.
#
# A "single standard" is one top-level bullet (•). Any nested sub-points under
# it (— dance: ..., — drama: ...) are folded into that same standard. So in the
# Content column, "elements in the arts, including but not limited to:" plus its
# dance/drama/music/visual-arts sub-bullets is ONE content item, and the next
# top-level bullet ("processes, materials, ...") is a SEPARATE item. Likewise a
# competency such as "Explore elements, processes, ... of the arts" is one item.

def _cluster_rows(words: list[dict], y_tol: float = 3.0) -> list[dict]:
    """Group words into visual rows, each with its words (x-sorted) and text."""
    rows: list[dict] = []
    for w in sorted(words, key=lambda w: w["top"]):
        for row in rows:
            if abs(row["top"] - w["top"]) <= y_tol:
                row["words"].append(w)
                break
        else:
            rows.append({"top": w["top"], "words": [w]})
    for r in rows:
        r["words"].sort(key=lambda w: w["x0"])
        r["text"] = " ".join(w["text"] for w in r["words"])
    rows.sort(key=lambda r: r["top"])
    return rows


def _cluster_lines(words: list[dict], y_tol: float = 3.0) -> list[tuple[float, str]]:
    return [(r["top"], r["text"]) for r in _cluster_rows(words, y_tol)]


def _clean_words(words: list[dict]) -> list[dict]:
    """Drop whole rows that are page header/footer noise (keeps real numbers)."""
    keep: list[dict] = []
    for r in _cluster_rows(words):
        if not NOISE.search(r["text"]):
            keep.extend(r["words"])
    return keep


def _lines_text(words: list[dict]) -> str:
    return "\n".join(t for _, t in _cluster_lines(words))


def _phrase_top(words: list[dict], phrase: str) -> float | None:
    """Top y of the first visual line that contains `phrase` (case-insensitive)."""
    p = phrase.lower()
    for top, text in _cluster_lines(words):
        if p in re.sub(r"\s+", " ", text.lower()):
            return top
    return None


def _topmost_word(words: list[dict], token: str) -> dict | None:
    cands = [w for w in words if w["text"].strip(":").lower() == token.lower()]
    return min(cands, key=lambda w: w["top"]) if cands else None


def _content_header(words: list[dict], cc: dict | None) -> dict | None:
    """The 'Content' column header: same row as 'Curricular', well to its right."""
    if not cc:
        return None
    cands = [w for w in words
             if w["text"].strip(":").lower() == "content"
             and abs(w["top"] - cc["top"]) < 8
             and w["x0"] > cc["x0"] + 40]
    return min(cands, key=lambda w: w["x0"]) if cands else None


def extract_grades(pdf_path: str, header: str) -> dict[tuple, dict]:
    """
    Return {grade_codes_tuple: {'codes', 'bigideas', 'comp', 'content', 'elab'}}.

    A page is a two-column standards page if it carries the
    'Curricular Competencies | Content' column headers (true for BOTH first and
    continuation pages, even though only first pages also say 'Learning
    Standards'). Those pages are split into left/right columns by x-position;
    everything else on a page is treated as single-column elaboration text.
    Grade bands (e.g. 'Grades 6-7') accumulate under one bucket and are expanded
    to individual grades later.
    """
    bands: dict[tuple, dict] = {}
    current: tuple | None = None
    # Any "Area of Learning: <name> <grade-label>" banner. If <name> is our
    # subject we switch to that grade/band; if it is a DIFFERENT area (e.g. the
    # Dance/Drama/Music/Visual Arts split that Arts Education uses at Grades 8-9)
    # we clear `current` so that out-of-subject content is not misattributed to
    # the last in-range grade.
    banner_re = re.compile(
        r"Area of Learning:\s*(.*?)\s+(" + GRADE_LABEL + r")\b", re.IGNORECASE)

    def bucket(key: tuple, codes: list[str]) -> dict:
        return bands.setdefault(
            key, {"codes": codes, "bigideas": [], "comp": [],
                   "content": [], "elab": []})

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            words = _clean_words(page.extract_words(
                x_tolerance=1.5, y_tolerance=3, use_text_flow=False))
            if not words:
                continue

            full = re.sub(r"\s+", " ", _lines_text(words))
            banner = banner_re.search(full)
            if banner:
                subject_name, label = banner.group(1), banner.group(2)
                if re.search(re.escape(header), subject_name, re.IGNORECASE):
                    codes = parse_grade_label(label)
                    if codes:
                        current = tuple(codes)
                        bucket(current, codes)
                else:
                    current = None  # a different area of learning started
            if current is None:
                continue

            cc = _topmost_word(words, "Curricular")
            ct = _content_header(words, cc)
            y_elab = _phrase_top(words, "Elaborations")

            # No paired column headers -> not a standards page (elaboration etc.).
            if ct is None:
                bucket(current, list(current))["elab"].append(_lines_text(words))
                continue

            header_top = cc["top"]
            split_x = ct["x0"] - 8
            band_bottom = y_elab if (y_elab is not None and y_elab > header_top) \
                else float(page.height)
            band = [w for w in words if header_top + 4 < w["top"] < band_bottom]

            left = [w for w in band if w["x0"] < split_x]
            right = [w for w in band if w["x0"] >= split_x]
            b = bucket(current, list(current))
            b["comp"].append(_lines_text(left))
            b["content"].append(_lines_text(right))

            # Big Ideas band (best effort; only when the header is present).
            if "BIG IDEAS" in full:
                y_title = _phrase_top(words, "Area of Learning") or 0.0
                bi = [w for w in words if y_title + 2 < w["top"] < header_top - 8]
                if bi:
                    b["bigideas"].append(_lines_text(bi))

            # Elaborations that begin lower on this same page.
            if y_elab is not None and y_elab >= band_bottom:
                el = [w for w in words if w["top"] >= band_bottom]
                if el:
                    b["elab"].append(_lines_text(el))

    return bands


def section(block: str, start_pat: str, end_pats: list[str]) -> str:
    """Return the slice of `block` between start_pat and the earliest end_pat."""
    m = re.search(start_pat, block, re.IGNORECASE)
    if not m:
        return ""
    start = m.end()
    end = len(block)
    for ep in end_pats:
        em = re.search(ep, block[start:], re.IGNORECASE)
        if em:
            end = min(end, start + em.start())
    return block[start:end]


def parse_grade(raw: dict, grade: str) -> dict:
    """Parse one grade's column-separated text chunks into structured standards."""
    comp_text = "\n".join(raw["comp"])
    cont_text = "\n".join(raw["content"])
    elab_text = "\n".join(raw["elab"])
    bi_text = "\n".join(raw["bigideas"])

    elab_anchor = rf"{DASH}\s*Elaborations"
    big_elab = parse_elaborations(
        section(elab_text, rf"Big Ideas\s*{elab_anchor}",
                [rf"Curricular Competencies\s*{elab_anchor}", rf"Content\s*{elab_anchor}"]))
    comp_elab = parse_elaborations(
        section(elab_text, rf"Curricular Competencies\s*{elab_anchor}",
                [rf"Content\s*{elab_anchor}"]))
    cont_elab = parse_elaborations(
        section(elab_text, rf"Content\s*{elab_anchor}",
                [r"Area of Learning", rf"Big Ideas\s*{elab_anchor}"]))

    competencies = parse_standard_block(comp_text, with_groups=True)
    contents = parse_standard_block(cont_text, with_groups=True)
    attach_elaborations(competencies, comp_elab)
    attach_elaborations(contents, cont_elab)

    return {
        "big_ideas": _clean_big_ideas(bi_text),
        "big_ideas_elaborations": big_elab,
        "curricular_competencies": [dataclasses.asdict(c) for c in competencies],
        "content": [dataclasses.asdict(c) for c in contents],
    }


def _clean_big_ideas(big: str) -> list[str]:
    """Big Ideas are a handful of short sentences stacked vertically."""
    lines = [l.strip() for l in big.splitlines()
             if l.strip() and not is_header_line(l) and l.strip().upper() != "BIG IDEAS"]
    ideas: list[str] = []
    cur = ""
    for l in lines:
        cur = (cur + " " + l).strip() if cur else l
        # A big idea typically ends a thought; the source rarely punctuates them,
        # so we split on blank-line grouping already done by splitlines + a
        # sentence-end heuristic.
        if l.endswith((".",)) or len(cur.split()) > 14:
            ideas.append(re.sub(r"\s+", " ", cur).strip(" ."))
            cur = ""
    if cur:
        ideas.append(re.sub(r"\s+", " ", cur).strip(" ."))
    # De-dup while preserving order.
    seen, out = set(), []
    for i in ideas:
        if i and i.lower() not in seen:
            seen.add(i.lower())
            out.append(i)
    return out


# --------------------------------------------------------------------------- #
# SQLite export
# --------------------------------------------------------------------------- #
def write_sqlite(db: dict, path: str) -> None:
    if os.path.exists(path):
        os.remove(path)
    con = sqlite3.connect(path)
    cur = con.cursor()
    cur.executescript(
        """
        CREATE TABLE subjects (slug TEXT PRIMARY KEY, name TEXT, url TEXT);
        CREATE TABLE standards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_slug TEXT, subject_name TEXT, grade TEXT,
            kind TEXT,                 -- 'big_idea' | 'competency' | 'content'
            group_name TEXT,
            text TEXT,
            elaborations TEXT          -- JSON object term->definition
        );
        CREATE INDEX idx_std ON standards(subject_slug, grade, kind);
        """
    )
    for name, sub in db["subjects"].items():
        cur.execute("INSERT INTO subjects VALUES (?,?,?)",
                    (sub["slug"], name, sub.get("url")))
        for grade, g in sub["grades"].items():
            for bi in g["big_ideas"]:
                cur.execute(
                    "INSERT INTO standards(subject_slug,subject_name,grade,kind,text,elaborations)"
                    " VALUES (?,?,?,?,?,?)",
                    (sub["slug"], name, grade, "big_idea", bi, "{}"))
            for c in g["curricular_competencies"]:
                cur.execute(
                    "INSERT INTO standards(subject_slug,subject_name,grade,kind,group_name,text,elaborations)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (sub["slug"], name, grade, "competency", c.get("group"),
                     c["text"], json.dumps(c["elaborations"], ensure_ascii=False)))
            for c in g["content"]:
                cur.execute(
                    "INSERT INTO standards(subject_slug,subject_name,grade,kind,text,elaborations)"
                    " VALUES (?,?,?,?,?,?)",
                    (sub["slug"], name, grade, "content", c["text"],
                     json.dumps(c["elaborations"], ensure_ascii=False)))
    con.commit()
    con.close()


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def grade_in_range(code: str, max_grade: int) -> bool:
    if code == "K":
        return True
    return code.isdigit() and int(code) <= max_grade


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the BC K-7 curriculum database.")
    ap.add_argument("--out-dir", default="out")
    ap.add_argument("--max-grade", type=int, default=7,
                    help="Highest grade to keep (default 7; K is always kept).")
    ap.add_argument("--subjects", nargs="*",
                    help="Limit to these subject slugs (default: all).")
    ap.add_argument("--layout", action="store_true",
                    help="Use pdfplumber layout mode (try if column parsing looks off).")
    ap.add_argument("--no-sqlite", action="store_true")
    ap.add_argument("--force-download", action="store_true")
    ap.add_argument("--no-raw", action="store_true", help="Skip writing raw text dumps.")
    args = ap.parse_args()

    out = args.out_dir
    pdf_dir = os.path.join(out, "pdfs")
    sub_dir = os.path.join(out, "subjects")
    raw_dir = os.path.join(out, "raw")
    for d in (out, pdf_dir, sub_dir, raw_dir):
        os.makedirs(d, exist_ok=True)

    wanted = set(s.lower() for s in args.subjects) if args.subjects else None
    db = {
        "source": "https://curriculum.gov.bc.ca",
        "generated": date.today().isoformat(),
        "max_grade": args.max_grade,
        "subjects": {},
    }

    summary_rows = []
    for name, cfg in SUBJECTS.items():
        if wanted and cfg["slug"].lower() not in wanted:
            continue
        print(f"\n=== {name} ===")
        got = download(name, cfg, pdf_dir, args.force_download)
        if not got:
            continue
        pdf_path, used_url = got

        bands = extract_grades(pdf_path, cfg["header"])

        def band_sort_key(codes_tuple):
            first = codes_tuple[0]
            return -1 if first == "K" else int(first)

        ordered = sorted(bands.values(), key=lambda b: band_sort_key(tuple(b["codes"])))

        if not args.no_raw:
            with open(os.path.join(raw_dir, f"{cfg['slug']}.txt"), "w", encoding="utf-8") as fh:
                fh.write(normalise(extract_text(pdf_path, layout=args.layout)))
            with open(os.path.join(raw_dir, f"{cfg['slug']}.columns.txt"), "w",
                      encoding="utf-8") as fh:
                for r in ordered:
                    fh.write(f"\n##### GRADES {'/'.join(r['codes'])} #####\n")
                    fh.write("----- COMPETENCIES -----\n" + "\n".join(r["comp"]) + "\n")
                    fh.write("----- CONTENT -----\n" + "\n".join(r["content"]) + "\n")
                    fh.write("----- ELABORATIONS -----\n" + "\n".join(r["elab"]) + "\n")

        grades: dict[str, dict] = {}
        for r in ordered:
            parsed = parse_grade(r, r["codes"][0])
            # A band (e.g. ADST "Grades 6-7") applies to each grade it covers.
            for code in r["codes"]:
                if grade_in_range(code, args.max_grade):
                    grades[code] = parsed
        grades = {c: grades[c] for c in sorted(grades, key=lambda c: -1 if c == "K" else int(c))}

        url_used = used_url or cfg["urls"][0]
        db["subjects"][name] = {"slug": cfg["slug"], "url": url_used, "grades": grades}

        with open(os.path.join(sub_dir, f"{cfg['slug']}.json"), "w", encoding="utf-8") as fh:
            json.dump(db["subjects"][name], fh, ensure_ascii=False, indent=2)

        for code, g in grades.items():
            summary_rows.append(
                (name, code, len(g["big_ideas"]),
                 len(g["curricular_competencies"]), len(g["content"]))
            )

    with open(os.path.join(out, "bc_curriculum.json"), "w", encoding="utf-8") as fh:
        json.dump(db, fh, ensure_ascii=False, indent=2)

    if not args.no_sqlite:
        write_sqlite(db, os.path.join(out, "bc_curriculum.sqlite"))

    # ---- summary table ----
    print("\n" + "=" * 64)
    print(f"{'Subject':<34}{'Gr':>4}{'Big':>5}{'Comp':>6}{'Cont':>6}")
    print("-" * 64)
    for name, code, b, comp, cont in summary_rows:
        flag = "  <-- check" if (comp == 0 or cont == 0) else ""
        print(f"{name[:33]:<34}{code:>4}{b:>5}{comp:>6}{cont:>6}{flag}")
    print("=" * 64)
    print(f"Subjects: {len(db['subjects'])}   "
          f"Grade rows: {len(summary_rows)}")
    print(f"\nWrote {os.path.join(out, 'bc_curriculum.json')}")
    if not args.no_sqlite:
        print(f"Wrote {os.path.join(out, 'bc_curriculum.sqlite')}")
    print("Any row flagged 'check' parsed empty for competencies or content; "
          "inspect out/raw/<slug>.txt for that subject and report back.")


if __name__ == "__main__":
    main()