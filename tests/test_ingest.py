"""Ingest pipeline: chunking, table rendering, the three checks, fact rows, the page cache and failed pages. No API calls."""
import base64
import copy
import json
import os
import threading
from types import SimpleNamespace

import pymupdf
import pytest

from analyst import ingest, llm, store


def col(period, kind="actual", unit=None):
    return {"period": period, "kind": kind, "unit": unit}


def row(label, values, metric=None, unit=None):
    return {"label": label, "metric": metric, "unit": unit, "values": values}


TABLE = {
    "type": "table", "title": "Quarterly Earnings Model", "unit": "INR m", "basis": "consolidated",
    "description": "Acme quarterly earnings model, INR m, 1QFY26 to FY27",
    "columns": [col("1QFY26"), col("2QFY26"), col("3QFY26"), col("4QFY26"), col("1QFY27"), col("2QFY27"), col("FY26"),
                col("FY27E", "forecast"), col("1QFY27", "broker_estimate"), col("1QFY27", "variance_pct")],
    "rows": [row("Gross Sales", [100, 110, 120, 130, 140, 150, 460, 600, 125, 12], metric="revenue"),
             row("Gross Sales: YoY Change (%)", [8.5, 9.5, 10.5, 11.5, 40, None, 12.5, 30.4, None, None],
                 metric="revenue_growth_yoy_pct", unit="%"),
             row("EBITDA", [10, 11, 12, 13, 14, 15, 46, 60, 13, None], metric="ebitda")],
}
CARD = {"company": "Acme", "aliases": "ACME, ACME IN", "sector": "Midcaps", "business": "Makes anvils",
        "report_type": "1QFY27 Results Update", "report_date": "3 August 2026", "reported_period": "1QFY27",
        "rating": "Buy", "cmp": 1942, "target_price": 2240, "upside_stated": "+15%", "valuation_basis": "35x FY28E EPS"}
PAGE_TEXT = " ".join(str(v) for r in TABLE["rows"] for v in r["values"] if v is not None)


def page(table=TABLE, card=None):
    return {"card": card, "blocks": [{"type": "text", "section": "Overview", "text": "Demand was weak."}, table]}


def changed(row_index, values):
    table = copy.deepcopy(TABLE)
    table["rows"][row_index]["values"] = values
    return table


# --- chunks -------------------------------------------------------------------------------------------------------

def test_split_text_keeps_every_word_under_the_cap():
    text = "\n".join([" ".join(f"a{i}" for i in range(400)), "short bullet", " ".join(f"b{i}" for i in range(150))])
    pieces = ingest.split_text(text)
    assert max(len(p.split()) for p in pieces) <= ingest.MAX_WORDS
    assert " ".join(pieces).split() == text.split()


def test_split_text_breaks_at_line_ends_when_lines_fit():
    assert ingest.split_text("one two\nthree four\nfive", cap=4) == ["one two\nthree four", "five"]


def test_chunks_merge_a_section_and_carry_search_text():
    blocks = [{"type": "text", "section": "Outlook", "text": "First bullet."},
              {"type": "text", "section": "Outlook", "text": "Second bullet."},
              TABLE,
              {"type": "chart", "title": "Exhibit 2: Revenue trend", "description": "Revenue bars: 1QFY27 56.1",
               "search": "Acme revenue trend 1QFY25 to 1QFY27"},
              {"type": "text", "section": "Risks", "text": "Steel prices."}]
    chunks = ingest.chunks_from("acme.pdf", 3, CARD, blocks)
    assert [c["id"] for c in chunks] == ["acme.pdf:3:0", "acme.pdf:3:1", "acme.pdf:3:2", "acme.pdf:3:3"]
    assert [c["kind"] for c in chunks] == ["text", "table", "chart", "text"]
    assert all(c["company"] == "Acme" and c["doc"] == "acme.pdf" and c["page"] == 3 for c in chunks)
    text, table, chart, _ = chunks
    assert text["content"] == "First bullet.\nSecond bullet."
    assert text["search_text"] == "Acme | 1QFY27 Results Update | Outlook\nFirst bullet.\nSecond bullet."
    assert table["section"] == "Quarterly Earnings Model"
    assert table["search_text"] == TABLE["description"] + "\nGross Sales, Gross Sales: YoY Change (%), EBITDA"
    assert chart["content"] == "Chart (values read from an image, approximate): Revenue bars: 1QFY27 56.1"
    assert chart["search_text"] == "Acme revenue trend 1QFY25 to 1QFY27"


def test_table_markdown():
    table = {**TABLE, "columns": TABLE["columns"][4:] + [col("YoY", unit="%")],
             "rows": [row("Gross Sales", [140, 150, 460, 600, 125, 12, 3.9]), row("Tax: Rate (%)", [25.3, None, 22.7, 25.2, 25.0, None, None])]}
    assert ingest.table_markdown(table, "1QFY27") == "\n".join([
        "**Quarterly Earnings Model** (unit: INR m; basis: consolidated)",
        # 2QFY27 is printed with no E but comes after the reported quarter, so it is marked as a forecast
        "| | 1QFY27 | 2QFY27 (E) | FY26 | FY27 (E) | 1QFY27 est. | var % | YOY (%) |",
        "|---|---|---|---|---|---|---|---|",
        "| Gross Sales | 140 | 150 | 460 | 600 | 125 | 12 | 3.9 |",
        "| Tax: Rate (%) | 25.3 |  | 22.7 | 25.2 | 25.0 |  |  |",
    ])


# --- checks -------------------------------------------------------------------------------------------------------

def test_a_correct_page_passes_every_check():
    checks = ingest.check_page(page(), PAGE_TEXT)
    assert checks["ok"] and checks["pass_rate"] == 1.0 and checks["values"] == 26
    assert checks["structure"] == checks["not_printed"] == checks["quarter_sum"] == []


def test_structure_check_catches_a_short_row():
    checks = ingest.check_page(page(changed(2, [10, 11, 12, 13, 14, 15, 46, 60, 13])), PAGE_TEXT)
    assert not checks["ok"]
    assert checks["structure"] == ["Quarterly Earnings Model / EBITDA: 9 values for 10 columns"]


def test_printed_check_catches_a_number_that_is_not_on_the_page():
    checks = ingest.check_page(page(changed(2, [10, 11, 12, 13, 14, 15, 46, 60, 9999, None])), PAGE_TEXT)
    assert checks["not_printed"] == [["Quarterly Earnings Model", "EBITDA", 9999]]
    assert checks["pass_rate"] == pytest.approx(25 / 26) and not checks["ok"]


def test_quarter_sum_check_catches_a_shifted_column():
    # every value is still printed on the page, so only the sum can tell that FY26 and FY27 swapped places
    checks = ingest.check_page(page(changed(0, [100, 110, 120, 130, 140, 150, 600, 460, 125, 12])), PAGE_TEXT)
    assert checks["not_printed"] == []
    assert checks["quarter_sum"] == [{"table": "Quarterly Earnings Model", "row": "Gross Sales", "year": "FY26",
                                      "quarters": [100, 110, 120, 130], "full_year": 600}]


def test_quarter_sum_skips_rows_that_are_not_money():
    assert ingest.quarter_sum(TABLE) == []  # the YoY (%) row does not add up, and must not be reported


# --- facts --------------------------------------------------------------------------------------------------------

def test_facts_from_a_table():
    table = {**TABLE, "columns": TABLE["columns"] + [col("YoY (%)", unit="%")],
             "rows": [{**r, "values": r["values"] + [7]} for r in TABLE["rows"]]}
    facts = ingest.facts_from(2, table)
    assert len(facts) == 29  # one per non-null cell
    assert all(f["page"] == 2 and f["source_table"] == "Quarterly Earnings Model" and f["basis"] == "consolidated" for f in facts)
    sales = [f for f in facts if f["row_label"] == "Gross Sales"]
    assert [(f["period"], f["kind"], f["value"], f["unit"]) for f in sales[4:]] == [
        ("1QFY27", "actual", 140, "INR m"), ("2QFY27", "actual", 150, "INR m"), ("FY26", "actual", 460, "INR m"),
        ("FY27E", "forecast", 600, "INR m"), ("1QFY27", "broker_estimate", 125, "INR m"),
        ("1QFY27", "variance_pct", 12, "%"), ("YoY (%)", "actual", 7, "%")]
    assert {f["metric"] for f in sales} == {"revenue"}
    growth = [f for f in facts if f["row_label"] == "Gross Sales: YoY Change (%)"]
    assert len(growth) == 8 and {f["unit"] for f in growth} == {"%"} and {f["metric"] for f in growth} == {"revenue_growth_yoy_pct"}


def test_facts_load_with_forecast_by_date_and_inr_m(tmp_path):
    conn = store.connect(tmp_path)
    store.add_facts(conn, "Acme", "acme.pdf", "1QFY27", ingest.facts_from(2, TABLE))
    got = {(r["row_label"], r["period"], r["kind"]): (r["value"], r["unit"], r["value_inr_m"]) for r in conn.execute("SELECT * FROM facts")}
    assert got[("Gross Sales", "2QFY27", "forecast")] == (150, "INR m", 150)  # printed with no E, after the reported quarter
    assert got[("Gross Sales", "FY27", "forecast")] == (600, "INR m", 600)
    assert got[("Gross Sales", "1QFY27", "variance_pct")] == (12, "%", None)
    assert got[("Gross Sales: YoY Change (%)", "1QFY27", "actual")] == (40, "%", None)


# --- run: cache and failed pages ----------------------------------------------------------------------------------

class FakeClient:
    """Answers each page from `answers`, keyed by a word printed on that page: page JSON, or a stop reason."""

    def __init__(self, answers):
        self.answers, self.calls, self.lock = answers, [], threading.Lock()
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **kwargs):
        document = kwargs["messages"][0]["content"][0]
        with self.lock, pymupdf.open(stream=base64.standard_b64decode(document["source"]["data"]), filetype="pdf") as one:
            text = one[0].get_text()  # under a lock: pymupdf is not thread-safe and run() calls this from its pool
        answer = next(a for key, a in self.answers.items() if key in text)
        self.calls.append(kwargs["model"])
        stop = answer if isinstance(answer, str) else "end_turn"
        content = [] if isinstance(answer, str) else [SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text=json.dumps(answer))]
        return SimpleNamespace(stop_reason=stop, content=content, usage=SimpleNamespace(input_tokens=1000, output_tokens=1000))


@pytest.fixture
def setup(tmp_path, monkeypatch):
    """A two-page report in tmp_path, the usage log redirected there, and a way to run ingest against a fake client."""
    docs = tmp_path / "docs"
    docs.mkdir()
    with pymupdf.open() as pdf_file:
        pdf_file.new_page().insert_text((50, 50), "FIRSTPAGE CMP 1942 TP 2240")
        pdf_file.new_page().insert_textbox((50, 50, 550, 700), "SECONDPAGE " + PAGE_TEXT)
        pdf_file.save(docs / "acme.pdf")
    monkeypatch.setattr(llm, "USAGE_LOG", tmp_path / "usage.jsonl")

    def run(answers):
        client = FakeClient(answers)
        monkeypatch.setattr(llm, "client", lambda: client)
        return ingest.run(docs_dir=docs, workers=2, data=tmp_path / "data"), client

    return run


FIRST = {"card": CARD, "blocks": [{"type": "text", "section": "Summary", "text": "Operating performance in line."}]}


def test_run_loads_the_stores_and_a_second_run_makes_no_calls(setup, tmp_path):
    out, client = setup({"FIRSTPAGE": FIRST, "SECONDPAGE": page()})
    assert (out["docs"], out["pages"], out["facts"], out["chunks"], out["failed_pages"]) == (1, 2, 26, 3, [])
    assert sorted(client.calls) == [ingest.DEFAULT_MODEL] * 2
    assert out["cost_usd"] == pytest.approx(2 * 1000 * (0.10 + 0.50) / 1e6)
    assert [(c["page"], c["ok"]) for c in out["checks"]] == [(1, True), (2, True)]

    again, client = setup({"FIRSTPAGE": FIRST, "SECONDPAGE": page()})
    assert client.calls == [] and again["cost_usd"] == 0
    assert (again["docs"], again["facts"], again["chunks"]) == (1, 26, 3)

    conn = store.connect(tmp_path / "data", readonly=True)
    count = lambda table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    assert (count("reports"), count("facts"), count("chunks"), count("chunks_fts")) == (1, 26, 3, 3)  # nothing duplicated
    assert store.collection(tmp_path / "data").count() == 3
    report = dict(conn.execute("SELECT * FROM reports").fetchone())
    assert (report["doc"], report["company"], report["rating"], report["cmp"], report["target_price"]) == ("acme.pdf", "Acme", "Buy", 1942, 2240)


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_a_refused_or_cut_off_page_is_reported_and_the_rest_is_loaded(setup, stop_reason):
    out, client = setup({"FIRSTPAGE": FIRST, "SECONDPAGE": stop_reason})
    assert out["failed_pages"] == [{"doc": "acme.pdf", "page": 2, "reason": stop_reason}]
    assert (out["docs"], out["facts"], out["chunks"]) == (1, 0, 1)
    # tried twice, then once on the stronger model
    assert sorted(client.calls) == sorted([ingest.DEFAULT_MODEL] * 3 + [ingest.ESCALATION_MODEL])

    out, client = setup({"FIRSTPAGE": FIRST, "SECONDPAGE": page()})  # a failed page is not cached: the next run reads it
    assert client.calls == [ingest.DEFAULT_MODEL] and out["failed_pages"] == [] and out["facts"] == 26


def test_a_report_with_no_card_is_reported_as_failed_and_not_loaded(setup, tmp_path):
    out, _ = setup({"FIRSTPAGE": "refusal", "SECONDPAGE": page()})
    assert (out["docs"], out["facts"], out["chunks"]) == (0, 0, 0)
    assert [(f["page"], f["reason"]) for f in out["failed_pages"]] == [
        (1, "refusal"), (1, "no report card on the first page; report not loaded")]
    assert store.connect(tmp_path / "data").execute("SELECT count(*) FROM reports").fetchone()[0] == 0


def test_a_page_failing_the_checks_is_escalated_stored_and_reported(setup):
    wrong = page(changed(2, [10, 11, 12, 13, 14, 15, 46, 60, 9999, None]))
    out, client = setup({"FIRSTPAGE": FIRST, "SECONDPAGE": wrong})
    assert sorted(client.calls) == sorted([ingest.DEFAULT_MODEL] * 3 + [ingest.ESCALATION_MODEL])
    assert out["failed_pages"] == [{"doc": "acme.pdf", "page": 2, "reason": "failed the checks; its values are stored"}]
    assert out["facts"] == 26  # the failing value is stored, not dropped
    assert out["checks"][1]["not_printed"] == [["Quarterly Earnings Model", "EBITDA", 9999]]


# --- live ---------------------------------------------------------------------------------------------------------

@pytest.mark.skipif(os.environ.get("ANALYST_LIVE") != "1", reason="calls the Claude API; set ANALYST_LIVE=1")
def test_live_apl_first_page_card(tmp_path):
    path = llm.ROOT / "docs" / "apl-financial-statement.pdf"
    number, text = ingest.pdf.content_pages(path)[0]
    result, _ = ingest.read_checked(llm.client(), path.name, number, text, ingest.page_pdf(path, number), ingest.DEFAULT_MODEL, tmp_path)
    card = result["data"]["card"]
    assert (card["rating"], card["cmp"], card["target_price"], card["sector"], card["reported_period"]) == ("Buy", 1942, 2240, "Midcaps", "1QFY27")
    assert "APAT" in card["aliases"] and result["checks"]["ok"]
