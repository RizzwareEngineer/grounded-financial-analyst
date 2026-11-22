import pytest

from analyst import store, tools

COMPANIES = {"Acme": "acme.pdf", "Beta": "beta.pdf", "Divi's Laboratories": "divis.pdf"}


def chunk(i, company, text, page=1):
    return {"id": i, "company": company, "doc": COMPANIES[company], "page": page, "section": "s",
            "kind": "text", "content": text, "search_text": text}


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    data = tmp_path_factory.mktemp("data")
    conn, coll = store.connect(data), store.collection(data)
    for company, doc in COMPANIES.items():
        store.add_report(conn, {"doc": doc, "company": company, "reported_period": "1QFY27", "rating": "Buy"})
    rows = lambda: [
        dict(page=3, row_label="EBITDA", metric="ebitda", period="1QFY27", kind="actual", value=9384.0, unit="INR m"),
        dict(page=3, row_label="EBITDA", metric="ebitda", period="1QFY27", kind="broker_estimate", value=8788.0, unit="INR m"),
        dict(page=3, row_label="EBITDA", metric="ebitda", period="2QFY27", kind="actual", value=9900.0, unit="INR m"),
        dict(page=4, row_label="EBITDA margin (%)", metric="ebitda_margin_pct", period="1QFY27", kind="actual", value=25.1, unit="%"),
    ]
    for company, doc in COMPANIES.items():
        store.add_facts(conn, company, doc, "1QFY27", rows())
    chunks = [
        chunk("a1", "Acme", "Realisation was INR 5,522 per tonne and EBITDA/MT improved sequentially.", 5),
        chunk("a2", "Acme", "Our dog ate the homework and the bus was late again.", 6),
        chunk("b1", "Beta", "Raw material costs fell as crude prices softened, helping margins.", 2),
        chunk("d1", "Divi's Laboratories", "Divi's custom synthesis pipeline remains strong.", 7),
    ]
    chunks += [chunk(f"f{i}{c[0]}", c, f"Filler paragraph {i} about warehouses, logistics and weather.")
               for i in range(12) for c in ("Acme", "Beta")]
    store.add_chunks(conn, coll, chunks)
    conn.commit()
    return conn, coll, data


def test_list_reports(env):
    conn, _, _ = env
    out = tools.list_reports(conn)
    assert [r["company"] for r in out] == ["Acme", "Beta", "Divi's Laboratories"]
    assert all(r["doc"] for r in out)


def test_list_labels(env):
    conn, _, _ = env
    out = tools.list_labels(conn, "Acme")
    by = {r["row_label"]: r for r in out}
    assert by["EBITDA"]["unit"] == "INR m" and by["EBITDA"]["metric"] == "ebitda"
    assert set(by["EBITDA"]["periods"].split(",")) == {"1QFY27", "2QFY27"}
    assert by["EBITDA margin (%)"]["unit"] == "%"


def test_list_labels_unknown(env):
    conn, _, _ = env
    out = tools.list_labels(conn, "Nope")
    assert "error" in out and out["valid_companies"] == ["Acme", "Beta", "Divi's Laboratories"]


def test_query_actual_and_estimate(env):
    _, _, data = env
    sql = "SELECT kind, value, doc, page FROM facts WHERE company='Acme' AND metric='ebitda' AND period='1QFY27' ORDER BY kind"
    rows = tools.query_numbers(sql, data)["rows"]
    assert [(r["kind"], r["value"]) for r in rows] == [("actual", 9384.0), ("broker_estimate", 8788.0)]
    assert rows[0]["doc"] == "acme.pdf" and rows[0]["page"] == 3


def test_query_arithmetic_and_trailing_semicolon(env):
    _, _, data = env
    out = tools.query_numbers("SELECT 9384.0/8788 - 1 AS beat;", data)
    assert out["rows"][0]["beat"] == pytest.approx(0.0678, abs=1e-4)


def test_query_zero_rows_hint(env):
    _, _, data = env
    out = tools.query_numbers("SELECT * FROM facts WHERE company='Nope'", data)
    assert out["rows"] == [] and out["valid_companies"] == ["Acme", "Beta", "Divi's Laboratories"] and "list_labels" in out["hint"]


def test_query_row_cap(env):
    _, _, data = env
    out = tools.query_numbers("WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i < 1000) SELECT i FROM n", data)
    assert len(out["rows"]) == tools.MAX_ROWS


def test_query_syntax_error(env):
    _, _, data = env
    out = tools.query_numbers("SELECT FROM WHERE", data)
    assert "error" in out and "rows" not in out


@pytest.mark.parametrize("sql", [
    "DELETE FROM facts", "UPDATE facts SET value=0", "INSERT INTO facts(company) VALUES ('x')", "DROP TABLE facts",
    "PRAGMA table_info(facts)", "ATTACH DATABASE 'x.db' AS x", "SELECT 1; SELECT 2", "SELECT 1; DELETE FROM facts",
    "WITH x AS (SELECT 1) DELETE FROM facts"])
def test_query_rejects_writes(env, sql):
    conn, _, data = env
    out = tools.query_numbers(sql, data)
    assert "error" in out and "rows" not in out
    assert conn.execute("SELECT count(*) FROM facts").fetchone()[0] == 12


def test_connection_is_read_only(env):
    _, _, data = env
    ro = store.connect(data, readonly=True)
    with pytest.raises(Exception, match="readonly"):
        ro.execute("DELETE FROM facts")


def test_search_company_filter(env):
    conn, coll, _ = env
    out = tools.search_text(conn, coll, "raw material costs", ["Beta"])
    assert out and {r["company"] for r in out} == {"Beta"}
    assert out[0]["doc"] == "beta.pdf" and out[0]["id"] == "b1"


def test_search_several_companies(env):
    conn, coll, _ = env
    out = tools.search_text(conn, coll, "margins", ["Acme", "Beta"], k=3)
    assert {r["company"] for r in out} == {"Acme", "Beta"}


def test_search_keyword_only(env, monkeypatch):
    conn, coll, _ = env
    monkeypatch.setattr(tools, "_vector", lambda *a: [])  # vectors find nothing, so only BM25 can answer
    for q in ("EBITDA/MT", "5,522"):
        assert [r["id"] for r in tools.search_text(conn, coll, q, ["Acme"], k=2)] == ["a1"]


def test_keyword_number_is_one_phrase(env):
    conn, _, _ = env  # "5,522" must not match every chunk containing a lone "5"
    conn.execute("INSERT INTO chunks_fts(search_text, content, id, company) VALUES ('item 5 here','item 5 here','x5','Acme')")
    assert tools._keyword(conn, "5,522", "Acme", 5) == ["a1"]
    conn.execute("DELETE FROM chunks_fts WHERE id = 'x5'")


def test_search_semantic_only(env):
    conn, coll, _ = env
    # shares no word with the chunk, which is about falling input costs
    out = tools.search_text(conn, coll, "cheaper commodity inputs lifted profitability", ["Beta"], k=1)
    assert out[0]["id"] == "b1"


def test_search_no_result_hint(env):
    conn, coll, _ = env
    out = tools.search_text(conn, coll, "anything", ["Nobody Ltd"])
    assert out["chunks"] == [] and "hint" in out


@pytest.mark.parametrize("q", ['"', "'", "(", "))", 'say "hi" (now)', "AND", "OR NOT", "NEAR(a b)", "a AND", "*", "-x", "col:val", "", "   ", "!!!"])
def test_search_odd_queries_do_not_crash(env, q):
    conn, coll, _ = env
    out = tools.search_text(conn, coll, q or " ", ["Acme"])
    assert isinstance(out, (list, dict))


def test_apostrophe_company_everywhere(env):
    conn, coll, data = env
    name = "Divi's Laboratories"
    assert tools.list_labels(conn, name)[0]["row_label"]
    assert any(r["company"] == name for r in tools.list_reports(conn))
    sql = "SELECT value FROM facts WHERE company='Divi''s Laboratories' AND kind='actual' AND period='1QFY27' AND metric='ebitda'"
    assert tools.query_numbers(sql, data)["rows"] == [{"value": 9384.0}]
    out = tools.search_text(conn, coll, "custom synthesis pipeline", [name])
    assert out[0]["company"] == name


# --- checker additions ---

def test_query_before_first_ingest_is_an_error_not_a_crash(tmp_path):
    assert "error" in tools.query_numbers("SELECT 1", tmp_path / "empty")


def test_runaway_query_is_stopped(env, monkeypatch):
    _, _, data = env
    monkeypatch.setattr(tools, "MAX_SECONDS", 0.2)
    out = tools.query_numbers("WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n) SELECT count(*) FROM n", data)
    assert "error" in out


@pytest.mark.parametrize("sql", [
    "SELECT load_extension('x')", "SELECT writefile('x.txt', 'y')", "SELECT readfile('/etc/passwd')",
    "WITH x AS (SELECT 1) INSERT INTO facts(company) SELECT 'z'", "VACUUM INTO 'copy.db'", "EXPLAIN DELETE FROM facts"])
def test_query_cannot_touch_files_or_write(env, sql):
    conn, _, data = env
    before = sorted(p.name for p in data.iterdir())
    assert "error" in tools.query_numbers(sql, data)
    assert conn.execute("SELECT count(*) FROM facts").fetchone()[0] == 12
    assert sorted(p.name for p in data.iterdir()) == before


@pytest.mark.parametrize("half", ["_vector", "_keyword"])
def test_company_filter_holds_in_each_half(env, monkeypatch, half):
    conn, coll, _ = env
    monkeypatch.setattr(tools, half, lambda *a: [])  # switch one half off; the other must still filter
    out = tools.search_text(conn, coll, "raw material costs EBITDA realisation", ["Acme"], k=20)
    assert out and {r["company"] for r in out} == {"Acme"}
    out = tools.search_text(conn, coll, "custom synthesis pipeline", ["Divi's Laboratories"], k=20)
    assert [r["id"] for r in out] == ["d1"]


def test_company_given_as_a_string(env):
    conn, coll, _ = env  # a model may send "Beta" where a list is expected; it must not be searched letter by letter
    out = tools.search_text(conn, coll, "raw material costs", "Beta")
    assert out[0]["id"] == "b1" and {r["company"] for r in out} == {"Beta"}


def test_read_page_returns_one_page_in_order_and_names_valid_companies_otherwise(env):
    conn = env[0]
    row = conn.execute("SELECT company, page FROM chunks LIMIT 1").fetchone()
    out = tools.read_page(conn, row["company"], row["page"])
    assert out and all(c["company"] == row["company"] and c["page"] == row["page"] and c["doc"] for c in out)
    missing = tools.read_page(conn, "Nobody Ltd", 1)
    assert "error" in missing and missing["valid_companies"]
