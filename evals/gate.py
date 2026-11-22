"""Checks on the ingested data that need no model. Run after `analyst ingest`: python evals/gate.py [data_dir]"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from analyst import store  # noqa: E402

data = Path(sys.argv[1]) if len(sys.argv) > 1 else store.DATA
conn = store.connect(data, readonly=True)
cards = json.loads((Path(__file__).resolve().parent.parent / "tests" / "cards.json").read_text())
problems = []


def expect(ok, message):
    if not ok:
        problems.append(message)


def values(doc, metric, period, kind="actual"):
    return [r[0] for r in conn.execute(
        "SELECT value FROM facts WHERE doc = ? AND metric = ? AND period = ? AND kind = ?", [doc, metric, period, kind])]


def printed(doc, value):
    return conn.execute("SELECT count(*) FROM facts WHERE doc = ? AND abs(value - ?) < 0.051", [doc, value]).fetchone()[0]


# 1. Every report is loaded and its card matches the hand-checked fixture.
got = {r["doc"]: dict(r) for r in conn.execute("SELECT * FROM reports")}
expect(len(got) == len(cards), f"{len(got)} reports loaded, expected {len(cards)}")
expect(len({g["company"] for g in got.values()}) == len(got), "two reports share one company name")
for card in cards:
    g = got.get(card["doc"])
    if not g:
        problems.append(f"{card['doc']}: not loaded")
        continue
    for key in ("rating", "cmp", "target_price", "sector", "reported_period"):
        a, b = g[key], card[key]
        same = str(a).strip().lower() == b.lower() if isinstance(b, str) else a is not None and abs(a - b) < 0.5
        expect(same, f"{card['doc']}: {key} is {a!r}, expected {b!r}")
    facts = conn.execute("SELECT count(*) FROM facts WHERE doc = ?", [card["doc"]]).fetchone()[0]
    chunks = conn.execute("SELECT count(*) FROM chunks WHERE doc = ?", [card["doc"]]).fetchone()[0]
    expect(facts > 0 and chunks > 0, f"{card['doc']}: {facts} facts, {chunks} chunks")

# 2. The sample questions that are pure lookups can be answered by SQL alone.
GSK, NALCO = "MotilalOswal_GLAXO_5765685.pdf", "MotilalOswal_NATIONALUM_5765693.pdf"
expect(9384 in values(GSK, "revenue", "1QFY27"), "S2: GSK 1QFY27 revenue 9,384 not stored as metric revenue / actual")
expect(8788 in values(GSK, "revenue", "1QFY27", "broker_estimate"), "S2: GSK revenue estimate 8,788 not stored as broker_estimate")
expect(printed(NALCO, 82882), "S1: NALCO FY28E EBITDA 82,882 not stored")
for doc, growth in [(NALCO, 39.3), ("MotilalOswal_ESCORTS_5765676.pdf", 28.0), (GSK, 16.5), ("MotilalOswal_UPL_5765696.pdf", 10.5)]:
    got_growth = values(doc, "revenue_growth_yoy_pct", "1QFY27")
    if not any(abs(v - growth) < 0.051 for v in got_growth):
        where = "printed somewhere but not under that metric" if printed(doc, growth) else "not stored at all"
        problems.append(f"S6: {doc} revenue growth {growth} is {where} (metric rows found: {got_growth})")

# 3. Forecasts are dated, not trusted from headers; a report with no quarter results has none.
expect(not values(GSK, "revenue", "2QFY27") and values(GSK, "revenue", "2QFY27", "forecast"),
       "GSK 2QFY27 revenue must be stored as a forecast only")
expect(not values("cello-financial-statement.pdf", "revenue", "1QFY27"), "Cello World must have no 1QFY27 actual revenue")

# 4. R3: the same revenue line in two tables of one report should agree once converted to INR m.
for doc in got:
    v = [r[0] for r in conn.execute(
        "SELECT DISTINCT value_inr_m FROM facts WHERE doc = ? AND metric = 'revenue' AND period = ? AND kind = 'actual' "
        "AND value_inr_m IS NOT NULL AND coalesce(basis, '') != 'standalone'", [doc, got[doc]["reported_period"]])]
    if v and max(v) > 1.05 * min(v):
        problems.append(f"R3: {doc} revenue for {got[doc]['reported_period']} differs across tables in INR m: {sorted(v)}")

counts = tuple(conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("reports", "facts", "chunks"))
print("reports %d, facts %d, chunks %d" % counts)
print("\n".join(["GATE FAILED:"] + ["- " + p for p in problems]) if problems else "GATE PASSED")
