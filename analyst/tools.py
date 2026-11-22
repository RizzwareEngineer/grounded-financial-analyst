"""The four tools the agent can call. Every result row carries its document, so citations come from data."""
import re
import sqlite3
import time
from contextlib import closing

from . import store

MAX_ROWS = 200
MAX_SECONDS = 5  # a model-written query that runs longer is stopped


def list_reports(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM reports ORDER BY company")]


def list_labels(conn, company):
    rows = conn.execute(
        "SELECT source_table, row_label, metric, unit, basis, group_concat(DISTINCT period) AS periods "
        "FROM facts WHERE company = ? GROUP BY source_table, row_label ORDER BY source_table", [company]).fetchall()
    if not rows:
        return {"error": f"No company named {company!r}.", "valid_companies": _companies(conn)}
    return [dict(r) for r in rows]


def query_numbers(sql, data=store.DATA):
    """Run one read-only SELECT against the facts and reports tables."""
    sql = sql.strip().rstrip(";")
    if not re.match(r"(?is)^(select|with)\b", sql) or ";" in sql:
        return {"error": "Only a single SELECT statement is allowed."}
    try:
        with closing(store.connect(data, readonly=True)) as conn:  # read-only connection: the model writes this SQL
            deadline = time.monotonic() + MAX_SECONDS
            conn.set_progress_handler(lambda: time.monotonic() > deadline, 10000)
            rows = [dict(r) for r in conn.execute(sql).fetchmany(MAX_ROWS)]
            conn.set_progress_handler(None, 0)
            if not rows:
                return {"rows": [], "hint": "0 rows. Check the company name against valid_companies, or call "
                        "list_labels(company) to see the row labels and periods that report has.",
                        "valid_companies": _companies(conn)}
    except sqlite3.Error as e:
        return {"error": str(e)}
    return {"rows": rows}


def search_text(conn, coll, query, companies=None, k=6):
    """Hybrid search over text chunks: vectors + BM25, merged by reciprocal rank. One search per company."""
    out = []
    for company in [companies] if isinstance(companies, str) else companies or [None]:
        ranked = _merge(_vector(coll, query, company, k * 2), _keyword(conn, query, company, k * 2))[:k]
        if not ranked:
            continue
        marks = ",".join("?" * len(ranked))
        found = {r["id"]: r for r in conn.execute(
            f"SELECT id, company, doc, page, section, kind, content FROM chunks WHERE id IN ({marks})", ranked)}
        out += [dict(found[i]) for i in ranked if i in found]
    return out or {"chunks": [], "hint": "No matching text. Try different words or drop the company filter."}


def _companies(conn):
    return [r[0] for r in conn.execute("SELECT company FROM reports ORDER BY company")]


def _vector(coll, query, company, n):
    res = coll.query(query_texts=[query], n_results=n, where={"company": company} if company else None)
    return res["ids"][0]


def _keyword(conn, query, company, n):
    words = re.findall(r"[A-Za-z0-9]+(?:[,./-][A-Za-z0-9]+)*", query)  # "5,522" and "EBITDA/MT" stay one phrase
    if not words:
        return []
    match = " OR ".join(f'"{w}"' for w in words)  # quoted so user text can't inject FTS5 operators
    sql = "SELECT id FROM chunks_fts WHERE chunks_fts MATCH ?" + (" AND company = ?" if company else "") + " ORDER BY bm25(chunks_fts) LIMIT ?"
    return [r[0] for r in conn.execute(sql, [match, company, n] if company else [match, n])]


def _merge(*rankings):
    scores = {}
    for ranking in rankings:
        for rank, chunk_id in enumerate(ranking):
            scores[chunk_id] = scores.get(chunk_id, 0) + 1 / (60 + rank)
    return sorted(scores, key=scores.get, reverse=True)
