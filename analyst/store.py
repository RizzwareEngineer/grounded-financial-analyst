"""Storage: SQLite holds the catalog, the numbers and a BM25 keyword index; Chroma holds the vectors."""
import re
import sqlite3
from pathlib import Path

import chromadb

DATA = Path(__file__).resolve().parent.parent / "data"

# Standard names for the lines most reports share. Anything else keeps metric = NULL and its printed row_label.
METRICS = [
    "revenue", "revenue_growth_yoy_pct", "gross_margin_pct", "ebitda", "ebitda_growth_yoy_pct",
    "ebitda_margin_pct", "pat", "adj_pat", "pat_growth_yoy_pct", "eps",
]
# actual: reported. forecast: a period after the reported one. broker_estimate / variance_pct: the
# broker's prior estimate for the reported quarter and the beat/miss against it.
KINDS = ["actual", "forecast", "broker_estimate", "variance_pct"]
BASES = ["consolidated", "standalone", "segment", "subsidiary"]
# Money totals must use one of these exact unit strings; any other unit is free text ("%", "x", "bp", "INR", "kt").
TO_INR_M = {"INR m": 1, "INR b": 1000}
MONEY_UNITS = [*TO_INR_M, "USD m", "USD b"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports(
  doc TEXT PRIMARY KEY, company TEXT, aliases TEXT, sector TEXT, business TEXT, report_type TEXT,
  report_date TEXT, reported_period TEXT, rating TEXT, cmp REAL, target_price REAL,
  upside_stated TEXT, valuation_basis TEXT);
CREATE TABLE IF NOT EXISTS facts(
  company TEXT, doc TEXT, page INTEGER, source_table TEXT, row_label TEXT, metric TEXT,
  period TEXT, kind TEXT, value REAL, unit TEXT, value_inr_m REAL, basis TEXT);
CREATE INDEX IF NOT EXISTS facts_lookup ON facts(company, metric, period);
CREATE TABLE IF NOT EXISTS chunks(
  id TEXT PRIMARY KEY, company TEXT, doc TEXT, page INTEGER, section TEXT, kind TEXT,
  content TEXT, search_text TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(search_text, content, id UNINDEXED, company UNINDEXED);
"""
REPORT_COLS = ["doc", "company", "aliases", "sector", "business", "report_type", "report_date",
               "reported_period", "rating", "cmp", "target_price", "upside_stated", "valuation_basis"]


def connect(data=DATA, readonly=False):
    data.mkdir(parents=True, exist_ok=True)
    db = data / "analyst.db"
    if readonly:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(db)
        conn.executescript(SCHEMA)
    conn.row_factory = sqlite3.Row
    return conn


def collection(data=DATA):
    return chromadb.PersistentClient(path=str(data / "chroma")).get_or_create_collection("chunks")


PERIOD = re.compile(r"(?:([1-4])Q|Q([1-4]))?FY(?:20)?(\d{2})E?")  # 1QFY27, Q1 FY27, FY27E, FY2027


def _parse(period):
    m = PERIOD.fullmatch(re.sub(r"\s+", "", (period or "").upper()))
    return (m[1] or m[2], m[3]) if m else None


def period_key(period):
    """'1QFY27' -> (27, 1); 'FY27' -> (27, 4): a full year ends with its fourth quarter, so a 4Q report's
    own full year is not a forecast."""
    p = _parse(period)
    return (int(p[1]), int(p[0] or 4)) if p else None


def clean_period(period):
    p = _parse(period)
    if p:
        return f"{p[0]}QFY{p[1]}" if p[0] else f"FY{p[1]}"
    return re.sub(r"E$", "", (period or "").strip().upper())


def clean_unit(unit):
    """'INR mn', 'INR M', 'Rs m' -> 'INR m'; 'INR bn' -> 'INR b'. Anything else is returned unchanged."""
    m = re.fullmatch(r"(INR|RS\.?|USD)\s*(M|MN|MLN|MILLION|B|BN|BILLION)", (unit or "").strip().upper())
    return f"{'USD' if m[1] == 'USD' else 'INR'} {m[2][0].lower()}" if m else unit


def _number(value):
    try:
        return float(value.replace(",", "") if isinstance(value, str) else value)
    except (TypeError, ValueError):
        return None


def fact_kind(period, reported_period, stated_kind):
    # Several reports print forecast quarters with no "E", so the date decides, not the header.
    a, b = period_key(period), period_key(reported_period)
    if a and b and a > b:
        return "forecast"
    return stated_kind if stated_kind in KINDS else "actual"


def add_report(conn, card):
    conn.execute(f"INSERT OR REPLACE INTO reports({','.join(REPORT_COLS)}) VALUES ({','.join('?' * len(REPORT_COLS))})",
                 [card.get(c) for c in REPORT_COLS])


def add_facts(conn, company, doc, reported_period, facts):
    """facts: dicts with page, row_label, period, value and optional source_table, metric, kind, unit, basis.
    A row whose value is not a number (an empty cell, 'n/a') is skipped."""
    rows = []
    for f in facts:
        value, unit = _number(f["value"]), clean_unit(f.get("unit"))
        if value is None:
            continue
        rows.append((company, doc, f["page"], f.get("source_table"), f["row_label"],
                     f.get("metric") if f.get("metric") in METRICS else None, clean_period(f["period"]),
                     fact_kind(f["period"], reported_period, f.get("kind")), value, unit,
                     value * TO_INR_M[unit] if unit in TO_INR_M else None,
                     f.get("basis") if f.get("basis") in BASES else None))
    conn.executemany("INSERT INTO facts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)


def add_chunks(conn, coll, chunks):
    """chunks: dicts with id, company, doc, page, section, kind ('text' | 'table' | 'chart'), content, search_text."""
    if not chunks:
        return
    conn.executemany("INSERT OR REPLACE INTO chunks VALUES (:id,:company,:doc,:page,:section,:kind,:content,:search_text)", chunks)
    conn.executemany("DELETE FROM chunks_fts WHERE id = :id", chunks)  # FTS has no primary key to replace on
    conn.executemany("INSERT INTO chunks_fts(search_text, content, id, company) VALUES (:search_text,:content,:id,:company)", chunks)
    coll.upsert(ids=[c["id"] for c in chunks], documents=[c["search_text"] for c in chunks],
                metadatas=[{"company": c["company"], "doc": c["doc"], "page": c["page"], "kind": c["kind"]} for c in chunks])


def clear_doc(conn, coll, doc):
    """Remove everything stored for one report, so re-ingesting it never leaves duplicates."""
    ids = [r[0] for r in conn.execute("SELECT id FROM chunks WHERE doc = ?", [doc])]
    conn.execute("DELETE FROM facts WHERE doc = ?", [doc])
    conn.execute("DELETE FROM reports WHERE doc = ?", [doc])
    conn.execute("DELETE FROM chunks WHERE doc = ?", [doc])
    if ids:
        conn.execute(f"DELETE FROM chunks_fts WHERE id IN ({','.join('?' * len(ids))})", ids)
        coll.delete(ids=ids)
