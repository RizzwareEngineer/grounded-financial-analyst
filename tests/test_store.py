import pytest

from analyst import store


@pytest.fixture
def db(tmp_path):
    conn = store.connect(tmp_path)
    yield conn, store.collection(tmp_path)
    conn.close()


def card(doc, company="Acme", rp="1QFY27"):
    return {"doc": doc, "company": company, "aliases": "Acme Ltd", "reported_period": rp, "rating": "Buy",
            "cmp": 100.0, "target_price": 120.0}


def fact(**kw):
    return {"page": 1, "row_label": "EBITDA", "period": "1QFY27", "value": 10.0, "unit": "INR m", **kw}


def chunk(i, doc="a.pdf", company="Acme", text="alpha beta"):
    return {"id": i, "company": company, "doc": doc, "page": 2, "section": "s", "kind": "text",
            "content": text, "search_text": text}


def test_schema_created(db):
    conn, _ = db
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert {"reports", "facts", "chunks", "chunks_fts"} <= names


def test_round_trip(db):
    conn, coll = db
    store.add_report(conn, card("a.pdf"))
    store.add_facts(conn, "Acme", "a.pdf", "1QFY27", [fact(metric="ebitda", kind="actual", basis="consolidated")])
    store.add_chunks(conn, coll, [chunk("c1"), chunk("c2", text="gamma")])
    assert conn.execute("SELECT rating, cmp FROM reports WHERE doc='a.pdf'").fetchone()[:] == ("Buy", 100.0)
    f = conn.execute("SELECT * FROM facts").fetchone()
    assert (f["company"], f["doc"], f["page"], f["metric"], f["period"], f["kind"], f["value"], f["basis"]) == \
        ("Acme", "a.pdf", 1, "ebitda", "1QFY27", "actual", 10.0, "consolidated")
    assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == 2
    assert conn.execute("SELECT id FROM chunks_fts WHERE chunks_fts MATCH 'gamma'").fetchone()[0] == "c2"
    assert coll.count() == 2
    store.add_chunks(conn, coll, [])  # no-op


@pytest.mark.parametrize("period, reported, stated, expected", [
    ("2QFY27", "1QFY27", "actual", "forecast"),
    ("FY27", "1QFY27", "actual", "forecast"),
    ("4QFY26", "1QFY27", "actual", "actual"),
    ("FY26", "1QFY27", "actual", "actual"),
    ("1QFY27", "FY26", "actual", "forecast"),
    ("1QFY27", "1QFY27", "broker_estimate", "broker_estimate"),
    ("1QFY27", "1QFY27", "variance_pct", "variance_pct"),
    ("3QFY27E", "1QFY27", "actual", "forecast"),
    ("Mar-27", "1QFY27", "forecast", "forecast"),
    ("garbage", "1QFY27", "actual", "actual"),
    ("1QFY27", "1QFY27", None, "actual"),
])
def test_fact_kind(period, reported, stated, expected):
    assert store.fact_kind(period, reported, stated) == expected


def test_trailing_e_stripped_and_kind_follows_date(db):
    conn, _ = db
    store.add_facts(conn, "Acme", "a.pdf", "1QFY27", [fact(period="FY28E", kind="actual")])
    assert conn.execute("SELECT period, kind FROM facts").fetchone()[:] == ("FY28", "forecast")


@pytest.mark.parametrize("unit, value, expected", [
    ("INR m", 5.0, 5.0), ("INR b", 5.0, 5000.0), ("%", 5.0, None), ("USD m", 5.0, None), (None, 5.0, None)])
def test_value_inr_m(db, unit, value, expected):
    conn, _ = db
    store.add_facts(conn, "Acme", "a.pdf", "1QFY27", [fact(unit=unit, value=value)])
    assert conn.execute("SELECT value_inr_m FROM facts").fetchone()[0] == expected


def test_invalid_metric_and_basis_become_null(db):
    conn, _ = db
    store.add_facts(conn, "Acme", "a.pdf", "1QFY27", [fact(metric="made_up", basis="weird")])
    assert conn.execute("SELECT metric, basis FROM facts").fetchone()[:] == (None, None)


def load(conn, coll, doc, company):
    store.add_report(conn, card(doc, company))
    store.add_facts(conn, company, doc, "1QFY27", [fact()])
    store.add_chunks(conn, coll, [chunk(f"{doc}-1", doc, company, f"{company} unique{doc[0]}")])


def counts(conn, coll, doc):
    return [conn.execute(f"SELECT count(*) FROM {t} WHERE doc = ?", [doc]).fetchone()[0] for t in ("reports", "facts", "chunks")] + \
        [conn.execute("SELECT count(*) FROM chunks_fts WHERE id LIKE ?", [doc + "%"]).fetchone()[0],
         len(coll.get(where={"doc": doc})["ids"])]


def test_clear_doc_leaves_others(db):
    conn, coll = db
    load(conn, coll, "a.pdf", "Acme")
    load(conn, coll, "b.pdf", "Beta")
    store.clear_doc(conn, coll, "a.pdf")
    assert counts(conn, coll, "a.pdf") == [0] * 5
    assert counts(conn, coll, "b.pdf") == [1] * 5


def test_reingest_has_no_duplicates(db):
    conn, coll = db
    for _ in range(2):
        store.clear_doc(conn, coll, "a.pdf")
        load(conn, coll, "a.pdf", "Acme")
    assert counts(conn, coll, "a.pdf") == [1] * 5


# --- checker additions ---

@pytest.mark.parametrize("period, stored", [
    ("FY2028", "FY28"), ("fy2028e", "FY28"), ("2Q FY27", "2QFY27"), ("Q2FY27", "2QFY27"), ("2qfy2027e", "2QFY27")])
def test_period_spellings_are_normalised_and_dated(db, period, stored):
    conn, _ = db
    store.add_facts(conn, "Acme", "a.pdf", "1qfy27", [fact(period=period, kind="actual")])
    assert conn.execute("SELECT period, kind FROM facts").fetchone()[:] == (stored, "forecast")


@pytest.mark.parametrize("period, reported, expected", [
    ("FY26", "4QFY26", "actual"),     # a 4Q report prints the finished full year
    ("4QFY26", "FY26", "actual"),
    ("FY27", "4QFY26", "forecast"),
    ("FY27", "3QFY27", "forecast"),
    (None, "1QFY27", "actual"),
    ("1QFY27", None, "actual"),
])
def test_fact_kind_edges(period, reported, expected):
    assert store.fact_kind(period, reported, "actual") == expected


@pytest.mark.parametrize("unit, stored, inr_m", [
    ("INR M", "INR m", 5.0), ("INR mn", "INR m", 5.0), ("inr million", "INR m", 5.0), ("Rs m", "INR m", 5.0),
    ("INR bn", "INR b", 5000.0), ("INR b ", "INR b", 5000.0), ("INRb", "INR b", 5000.0),
    ("USD mn", "USD m", None), ("bp", "bp", None), ("INR", "INR", None), ("x", "x", None)])
def test_unit_spellings(db, unit, stored, inr_m):
    conn, _ = db
    store.add_facts(conn, "Acme", "a.pdf", "1QFY27", [fact(unit=unit, value=5.0)])
    assert conn.execute("SELECT unit, value_inr_m FROM facts").fetchone()[:] == (stored, inr_m)


def test_blank_and_text_values(db):
    conn, _ = db  # one empty cell must not abort the whole report, and "4,113" must not be stored as text
    store.add_facts(conn, "Acme", "a.pdf", "1QFY27", [fact(value=None), fact(value="n/a"), fact(value="4,113", unit="INR b"), fact(value=7)])
    assert [r[:] for r in conn.execute("SELECT value, value_inr_m FROM facts ORDER BY value")] == [(7.0, 7.0), (4113.0, 4113000.0)]


def test_add_chunks_twice_keeps_one_keyword_row(db):
    conn, coll = db
    for text in ("old words", "new words"):
        store.add_chunks(conn, coll, [chunk("c1", text=text)])
    assert [r[0] for r in conn.execute("SELECT search_text FROM chunks_fts WHERE id = 'c1'")] == ["new words"]
    assert coll.count() == 1
