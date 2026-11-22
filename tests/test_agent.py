"""Agent loop and answer check. Everything runs offline against a scripted client except the tests marked live."""
import itertools
import os
from types import SimpleNamespace

import pytest

import fixture_store
from analyst import agent, llm, store
from fixture_store import ESCORTS, GSK, MUTHOOT, NALCO, UPL

USAGE = SimpleNamespace(input_tokens=1000, output_tokens=100)
GSK_SQL = "SELECT doc, kind, value, unit FROM facts WHERE company = 'GSK Pharma' AND metric = 'revenue' AND period = '1QFY27'"
BEAT_SQL = ("SELECT a.doc, ROUND((a.value - e.value) * 100.0 / e.value, 1) AS beat_pct FROM facts a JOIN facts e "
            "ON e.doc = a.doc AND e.metric = a.metric AND e.period = a.period AND e.kind = 'broker_estimate' "
            "WHERE a.company = 'GSK Pharma' AND a.metric = 'revenue' AND a.kind = 'actual'")
REVENUE = {"text": "GSK Pharma reported 1QFY27 revenue of INR9,384m against the broker's estimate of INR8,788m.", "sources": [GSK]}
BEAT = {"text": "That is a 6.8% beat.", "sources": [GSK]}


# ---- the number tracer -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, tool_numbers", [
    ("a ~7% beat", {6.782}),                                  # rounded to the printed precision
    ("a 6.8% beat", {6.782}),
    ("a 6.78% beat", {6.782}),
    ("up 6.8%", {6.75}),                                      # a half rounds either way
    ("revenue of INR9.4b", {9384}),                           # INR m printed as INR b
    ("revenue of INR9.38b", {9384}),
    ("revenue of INR101,800m", {101.8}),                      # INR b printed as INR m
    ("grew 16.5% YoY", {0.165}),                              # a ratio printed as a percentage
    ("margin fell 370bp", {3.7}),
    ("INR9,384m and INR7,06,540m", {9384, 706540}),           # thousands separators, Indian grouping too
    ("EBITDA fell 3.7%", {-3.7}),                             # sign ignored
    ("a loss of INR-120m", {120}),
    ("between 1.5-2.0% more", {1.5, 2}),
    ("TP of INR380 at 6.5x FY28E EV/EBITDA", {380, 6.5}),
    ("up 16.5 per cent to INR9.4b", {0.165, 9384}),
])
def test_traced(text, tool_numbers):
    assert agent.untraced(text, tool_numbers) == []


@pytest.mark.parametrize("text, tool_numbers, bad", [
    ("revenue of INR9,500m", {9384, 8788}, ["9,500"]),        # invented
    ("a 6.9% beat", {6.782}, ["6.9"]),                        # wrong at the printed precision
    ("revenue of INR9.39b", {9384}, ["9.39"]),
    ("up 8%", {9384, 16.5}, ["8"]),
    ("INR9,384m, up 17.5%", {9384, 16.5}, ["17.5"]),          # one good number does not cover a bad one
    ("revenue of 12", set(), ["12"]),                         # nothing to trace to
    ("a 9% beat", {9384, 8788}, ["9"]),                       # INR m against INR b is for money, never for a rate
    ("up 3 %", {3136}, ["3"]),
    ("margin fell 9bp", {9384}, ["9"]),
    ("valued at 9x earnings", {9384}, ["9"]),
])
def test_untraced(text, tool_numbers, bad):
    assert agent.untraced(text, tool_numbers) == bad


@pytest.mark.parametrize("text", [
    "1QFY27, 2QFY27, 4QFY26, 2HFY27, 1HFY27, 9MFY26, FY27, FY28E, FY2027, CY25, Q1FY27, 1Q FY27, FY 27",
    "over FY26-28E and FY27/28",
    "in 1Q and 2H",
    "2026-08-03, Apr'26, Apr’26, Mar-26, Aug 2026, August 2026, 3 August 2026, 3rd August 2026, August 3, 2026, 2 Aug",
    "the corpus has 3 reports labelled Healthcare; 2 of 17 and 2 of the 17 companies; 5 documents; 4 sources",
    "ranked 1st, 2nd and 3rd, #1 overall",
    "1. NALCO leads",
    "(2) Escorts Kubota follows",
    "see MotilalOswal_UPL_5765696.pdf and [MuthootFinance_MotilalOswal_02082026.pdf]",
    "three reports and eight quarters",
    "see page 3 and pages 4",
])
def test_not_numbers(text):
    assert agent.number_tokens(text) == []


@pytest.mark.parametrize("text, tokens", [
    ("1QFY27 revenue was 9,384", ["9,384"]),
    ("FY28E EBITDA of INR82,882m", ["82,882"]),
    ("Margin 27.2% in 1QFY27", ["27.2"]),                     # "Mar" inside a word is not a month
    ("margins may 12% improve", ["12"]),                      # "may" the verb is not a month
    ("May 27.5% of sales", ["27.5"]),
    ("FY27-12% growth", ["12"]),
    ("a price hike of 1.5% in Apr'26", ["1.5"]),
    ("up 28.0%. Next", ["28.0"]),
    ("9384. That is all", ["9384"]),
    ("369, 380 and 3%", ["369", "380", "3"]),
    ("3 reports show 16.5% and 2 of 17 show 9%", ["16.5", "9"]),
])
def test_number_tokens(text, tokens):
    assert agent.number_tokens(text) == tokens


def test_collect_reads_docs_and_numbers_from_any_shape():
    docs, numbers = set(), set()
    result = {"rows": [{"doc": GSK, "page": 3, "value": 9384, "beat": 6.78, "ok": True, "period": "1QFY27", "source": "x.pdf"},
                       {"id": f"{UPL}:7:12", "d": UPL, "page": 7,
                        "content": "first high-teen growth after eight quarters; up 16.5% to INR9.4b on 3 August 2026"}]}
    agent.collect(result, {GSK, UPL, NALCO}, docs, numbers)
    assert docs == {GSK, UPL}                                 # a known document under any column name; unknown names never
    assert numbers == {9384, 6.78, 8, 16.5, 9.4}              # a page number or a chunk id is not a figure


# ---- source check, rendering -------------------------------------------------------------------------------------

def test_check_sources_and_numbers():
    claims = [
        {"text": "Revenue was INR9,384m.", "sources": [GSK, "made_up.pdf", GSK]},
        {"text": "Revenue was INR9,384m.", "sources": ["made_up.pdf"]},
        {"text": "Revenue was INR9,384m.", "sources": [UPL]},     # a real report, but no tool returned it in this run
        {"text": "Revenue was INR9,384m.", "sources": []},
        {"text": "Revenue was INR9,999m.", "sources": [GSK]},
    ]
    kept, failures = agent.check(claims, {GSK}, {9384})
    assert kept == [{"text": "Revenue was INR9,384m.", "sources": [GSK]}]
    assert [f["check"] for f in failures] == ["cites no document returned by your tools"] * 3 + ["numbers not in your tool results: 9,999"]


def test_render():
    kept = [{"text": "A is up 5%.", "sources": [GSK]}, {"text": "B is up 6%.", "sources": [UPL, NALCO]}]
    assert agent.render(kept, 0, "") == f"A is up 5%. [{GSK}]\nB is up 6%. [{UPL}] [{NALCO}]"
    lines = agent.render(kept, 2, "Hindalco has no report.").splitlines()
    assert lines[2] == "Note: 2 statement(s) were removed because they could not be traced to a source."
    assert lines[3] == "Not in the corpus: Hindalco has no report."


# ---- the loop, driven by a scripted client -----------------------------------------------------------------------

class FakeClient:
    """Stands in for the Anthropic client: returns scripted responses and keeps what it was sent."""

    def __init__(self, script):
        self.script, self.requests, self.messages = iter(script), [], self

    def create(self, **kwargs):
        self.requests.append({**kwargs, "messages": [{**m, "content": list(m["content"])} for m in kwargs["messages"]]})
        return next(self.script)


_ids = itertools.count()


def use(*calls):
    return SimpleNamespace(stop_reason="tool_use", usage=USAGE, content=[
        SimpleNamespace(type="thinking", thinking=""),
        *[SimpleNamespace(type="tool_use", id=f"toolu_{next(_ids)}", name=name, input=args) for name, args in calls]])


def say(text):
    return SimpleNamespace(stop_reason="end_turn", usage=USAGE, content=[SimpleNamespace(type="text", text=text)])


def submit(*claims, refused=False, missing=""):
    return use(("submit_answer", {"refused": refused, "claims": list(claims), "missing": missing}))


@pytest.fixture(scope="module")
def data(tmp_path_factory):
    path = tmp_path_factory.mktemp("store")
    fixture_store.build(path)
    return path


@pytest.fixture
def run(data, tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "USAGE_LOG", tmp_path / "usage.jsonl")
    monkeypatch.setattr(agent, "REVIEW", False)  # the loop tests script exact rounds; the review round has its own test

    def run(*script):
        client = FakeClient(script)
        monkeypatch.setattr(llm, "client", lambda: client)
        return agent.ask("a question", data=data), client.requests
    return run


def last_user(request):
    return request["messages"][-1]["content"]


def test_fixture_store(data):
    conn = store.connect(data, readonly=True)
    assert conn.execute("SELECT count(*) FROM reports").fetchone()[0] == 5
    kinds = dict(conn.execute("SELECT period, kind FROM facts WHERE company = 'GSK Pharma' AND metric = 'revenue' AND kind != 'broker_estimate'"))
    assert kinds == {"1QFY27": "actual", "2QFY27": "forecast"}
    assert conn.execute("SELECT value_inr_m FROM facts WHERE company = 'UPL' AND metric = 'revenue'").fetchone()[0] == pytest.approx(101800)
    assert conn.execute("SELECT count(*) FROM facts WHERE company = 'Muthoot Finance' AND metric IS NOT NULL").fetchone()[0] == 0


def test_system_prompt_is_built_from_the_database(data):
    prompt = agent.system_prompt(store.connect(data, readonly=True))
    for needle in ["GSK Pharma", "Healthcare", MUTHOOT, "Escorts Kubota | Escorts,EKL | Automobile | 1QFY27 | Neutral | " + ESCORTS,
                   *store.METRICS, *store.KINDS, *store.BASES, *store.MONEY_UNITS, "value_inr_m", "submit_answer"]:
        assert needle in prompt
    assert prompt.count("SELECT") >= 4


def test_answer_with_several_tool_calls_in_one_round(run):
    result, requests = run(
        use(("query_numbers", {"sql": GSK_SQL}), ("query_numbers", {"sql": BEAT_SQL}),
            ("search_text", {"query": "high-teen growth", "companies": ["GSK Pharma"], "k": 2})),
        submit(REVENUE, BEAT, {"text": "It was the first high-teen growth after eight quarters.", "sources": [GSK]}))
    assert len(requests) == 2
    results = last_user(requests[1])
    assert [r["type"] for r in results] == ["tool_result"] * 3 and not any(r["is_error"] for r in results)
    assert requests[1]["messages"][1]["content"][0].type == "thinking"      # the assistant turn goes back exactly as returned
    assert result["refused"] is False and result["cited"] == [GSK]
    assert result["answer"].splitlines() == [f"{REVENUE['text']} [{GSK}]", f"{BEAT['text']} [{GSK}]",
                                             f"It was the first high-teen growth after eight quarters. [{GSK}]"]
    assert [(t["round"], t["tool"]) for t in result["trace"]] == [(1, "query_numbers"), (1, "query_numbers"), (1, "search_text")]
    assert result["trace"][0]["summary"] == "2 rows" and result["trace"][2]["summary"].startswith("chunks: ")
    assert result["cost_usd"] == pytest.approx(2 * llm.cost(agent.MODEL, USAGE))


def test_request_shape(run):
    _, requests = run(submit(refused=True))
    request = requests[0]
    assert request["model"] == "claude-opus-5-5" and request["max_tokens"] == 16000
    assert not {"temperature", "top_p", "top_k", "tool_choice", "thinking"} & set(request)
    assert request["system"][0]["cache_control"] == {"type": "ephemeral"} and request["cache_control"] == {"type": "ephemeral"}
    assert [t["name"] for t in request["tools"]] == ["list_reports", "list_labels", "query_numbers", "search_text", "read_page", "submit_answer"]
    assert request["tools"][-1]["strict"] is True
    assert request["messages"] == [{"role": "user", "content": [{"type": "text", "text": "a question"}]}]


def test_every_tool_is_dispatched(run):
    result, requests = run(
        use(("list_reports", {}), ("list_labels", {"company": "Muthoot Finance"}),
            ("query_numbers", {"sql": GSK_SQL}), ("search_text", {"query": "raw-material cost inflation"})),
        submit(refused=True))
    assert [t["summary"] for t in result["trace"][:3]] == ["5 rows", "2 rows", "2 rows"]
    assert "Total operating income" in last_user(requests[1])[1]["content"]
    assert ESCORTS in last_user(requests[1])[3]["content"]


def test_a_failing_tool_reports_its_error_and_the_run_goes_on(run, monkeypatch):
    monkeypatch.setattr(agent.tools, "search_text", lambda *args: 1 / 0)
    result, requests = run(
        use(("search_text", {"query": "margins"}), ("list_labels", {}), ("no_such_tool", {}), ("query_numbers", {"sql": "DELETE FROM facts"}),
            ("query_numbers", {"sql": GSK_SQL})),
        submit(REVENUE))
    results = last_user(requests[1])
    assert [r["is_error"] for r in results] == [True, True, True, True, False]
    assert "ZeroDivisionError" in results[0]["content"] and "KeyError" in results[1]["content"] and "Unknown tool" in results[2]["content"]
    assert result["trace"][0]["summary"].startswith("error: ZeroDivisionError")
    assert result["refused"] is False and result["cited"] == [GSK]


def test_a_page_number_does_not_trace_an_invented_figure(run):
    invented = {"text": "That is a 1% beat, or INR1,000m.", "sources": [GSK]}
    result, _ = run(use(("query_numbers", {"sql": GSK_SQL.replace("doc,", "doc, page,")})), submit(invented), submit(invented))
    assert result["refused"] is True and result["cited"] == []


def test_a_query_result_with_a_content_column_does_not_crash(run):
    result, _ = run(use(("query_numbers", {"sql": "SELECT doc, row_label AS content FROM facts WHERE company = 'GSK Pharma'"}),
                        ("query_numbers", {"sql": "SELECT id, content FROM chunks ORDER BY id"})), submit(refused=True))
    assert [t["summary"] for t in result["trace"]] == ["4 rows", "chunks: " + ", ".join(f"{d}:p1:c{i}" for i, d in enumerate([ESCORTS, GSK, NALCO]))]


def test_round_cap(run):
    result, requests = run(*itertools.repeat(use(("list_labels", {"company": "NALCO"})), 20))
    assert len(requests) == agent.MAX_ROUNDS == 8
    assert "last round" in last_user(requests[7])[-1]["text"]
    assert not any("last round" in str(last_user(r)) for r in requests[:7])
    assert result == {"answer": "Not in the corpus: " + agent.NO_ANSWER, "cited": [], "refused": True,
                      "trace": result["trace"], "cost_usd": pytest.approx(8 * llm.cost(agent.MODEL, USAGE))}
    assert len(result["trace"]) == 8


def test_free_text_is_never_the_answer(run):
    result, requests = run(use(("query_numbers", {"sql": GSK_SQL})), say("Revenue was INR9,384m, trust me."), submit(REVENUE))
    assert len(requests) == 3 and "submit_answer" in last_user(requests[2])[0]["text"]
    assert "trust me" not in result["answer"] and result["cited"] == [GSK]

    result, requests = run(say("Revenue was INR9,384m."), say("Really, it was."), submit(REVENUE))
    assert len(requests) == 2                                 # asked once, not twice
    assert result["refused"] is True and result["answer"] == "Not in the corpus: " + agent.NO_ANSWER


def test_one_repair_round(run):
    result, requests = run(use(("query_numbers", {"sql": GSK_SQL})), submit(REVENUE, BEAT),
                           use(("query_numbers", {"sql": BEAT_SQL})), submit(REVENUE, BEAT))
    assert len(requests) == 4
    repair = last_user(requests[2])[0]
    assert repair["is_error"] and "That is a 6.8% beat." in repair["content"] and "6.8" in repair["content"]
    assert REVENUE["text"] not in repair["content"]           # only the failing claims go back
    assert result["answer"].splitlines() == [f"{REVENUE['text']} [{GSK}]", f"{BEAT['text']} [{GSK}]"]
    assert [t for t in result["trace"] if "check" in t] == [
        {"round": 2, "claim": BEAT["text"], "check": "numbers not in your tool results: 6.8"}]


def test_the_first_answer_is_sent_back_once_for_a_completeness_review(run, monkeypatch):
    monkeypatch.setattr(agent, "REVIEW", True)
    result, requests = run(use(("query_numbers", {"sql": GSK_SQL})), submit(REVENUE),
                           use(("query_numbers", {"sql": BEAT_SQL})), submit(REVENUE, BEAT))
    assert len(requests) == 4                                 # the second submission is final: no second review
    review = last_user(requests[2])[0]
    assert review["content"] == agent.REVIEW_TEXT and not review["is_error"]
    assert result["answer"].splitlines() == [f"{REVENUE['text']} [{GSK}]", f"{BEAT['text']} [{GSK}]"]

    refusal, requests = run(submit(refused=True))             # a refusal is not sent back
    assert len(requests) == 1 and refusal["refused"]


def test_claims_that_still_fail_are_dropped_with_a_note(run):
    result, requests = run(use(("query_numbers", {"sql": GSK_SQL})), submit(REVENUE, BEAT), submit(REVENUE, BEAT), submit(REVENUE))
    assert len(requests) == 3                                 # one repair round only
    assert result["refused"] is False and result["cited"] == [GSK]
    assert result["answer"].splitlines() == [
        f"{REVENUE['text']} [{GSK}]", "Note: 1 statement(s) were removed because they could not be traced to a source."]


def test_no_surviving_claim_is_a_refusal(run):
    invented = {"text": "UPL's revenue was INR99,999m.", "sources": [UPL]}
    result, requests = run(use(("query_numbers", {"sql": GSK_SQL})), submit(invented), submit(invented, missing="UPL's revenue."))
    assert len(requests) == 3
    assert result["refused"] is True and result["cited"] == [] and result["answer"] == "Not in the corpus: UPL's revenue."


def test_model_refusal(run):
    result, requests = run(submit(refused=True, missing="HDFC Bank is not one of the reports."))
    assert len(requests) == 1 and result["trace"] == []
    assert result["answer"] == "Not in the corpus: HDFC Bank is not one of the reports." and result["refused"] is True

    result, _ = run(use(("query_numbers", {"sql": GSK_SQL})), submit(REVENUE, refused=True))
    assert result["refused"] is True and result["cited"] == [] and result["answer"] == "Not in the corpus: " + agent.NO_ANSWER


def test_partial_answer_ends_with_what_is_missing(run):
    result, _ = run(use(("query_numbers", {"sql": GSK_SQL})), submit(REVENUE, missing="Hindalco has no report in the corpus."))
    assert result["answer"].splitlines() == [f"{REVENUE['text']} [{GSK}]", "Not in the corpus: Hindalco has no report in the corpus."]

    # The missing text is shown to the user too, so an invented number there is not let through.
    result, _ = run(use(("query_numbers", {"sql": GSK_SQL})), submit(REVENUE, missing="The repo rate, which is 5.25%."))
    assert result["answer"] == f"{REVENUE['text']} [{GSK}]"


def test_no_repair_on_the_last_round(run):
    result, requests = run(*itertools.repeat(use(("query_numbers", {"sql": GSK_SQL})), 7), submit(REVENUE, BEAT))
    assert len(requests) == 8 and result["refused"] is False
    assert result["answer"].endswith("Note: 1 statement(s) were removed because they could not be traced to a source.")


@pytest.mark.parametrize("stop_reason", ["max_tokens", "refusal"])
def test_a_stopped_response_does_not_crash(run, stop_reason):
    result, requests = run(SimpleNamespace(stop_reason=stop_reason, usage=USAGE, content=[]))
    assert len(requests) == 1 and result["refused"] is True
    assert result["trace"] == [{"round": 1, "check": f"model stopped: {stop_reason}"}]


# ---- live: the real model against the fixture store (ANALYST_LIVE=1; at most 8 questions and 1.50 US dollars) ------

LIVE_BUDGET_USD = 1.50


def has(answer, *options):
    return any(o in answer.replace(",", "") for o in options)


def in_order(answer, *parts):
    spots = [answer.find(p) for p in parts]
    return -1 not in spots and spots == sorted(spots)


LIVE = {
    "beat": ("By how much did GSK Pharma's 1QFY27 revenue exceed the broker's estimate, and what was the YoY growth?",
             lambda r: not r["refused"] and r["cited"] == [GSK] and has(r["answer"], "9384", "9.4") and has(r["answer"], "8788", "8.8")
             and has(r["answer"], "16.5") and has(r["answer"], "6.8", "7%", "596")),
    "ranking": ("Rank NALCO, Escorts Kubota, GSK Pharma and UPL by 1QFY27 YoY revenue growth",
                lambda r: not r["refused"] and set(r["cited"]) == {NALCO, ESCORTS, GSK, UPL} and in_order(r["answer"], "39.3", "28", "16.5", "10.5")),
    "why": ("Why did Escorts Kubota's gross margin decline in 1QFY27?",
            lambda r: not r["refused"] and ESCORTS in r["cited"] and "raw" in r["answer"].lower() and has(r["answer"], "27.2")),
    "missing_label": ("Compare Muthoot Finance's and GSK Pharma's revenue growth last quarter",
                      lambda r: not r["refused"] and set(r["cited"]) == {MUTHOOT, GSK} and has(r["answer"], "33.1") and has(r["answer"], "16.5")),
    "absent_company": ("How did HDFC Bank do last quarter?", lambda r: r["refused"] and r["cited"] == []),
    "not_reported": ("What revenue did GSK Pharma report for 2QFY27?",
                     lambda r: r["refused"] and (not has(r["answer"], "11275") or "forecast" in r["answer"].lower())),
    "false_premise": ("Why did the broker downgrade NALCO to Sell?",
                      lambda r: not r["refused"] and NALCO in r["cited"] and "Neutral" in r["answer"]),
    "general_knowledge": ("What is EV/EBITDA?", lambda r: r["refused"] and r["cited"] == []),
}


@pytest.fixture(scope="module")
def live_start():
    return llm.spent("ask")


@pytest.mark.skipif(os.environ.get("ANALYST_LIVE") != "1", reason="live model calls: set ANALYST_LIVE=1")
@pytest.mark.parametrize("name", LIVE)
def test_live(name, data, live_start):
    assert len(LIVE) <= 8
    assert llm.spent("ask") - live_start < LIVE_BUDGET_USD, "live budget used up"
    question, passes = LIVE[name]
    result = agent.ask(question, data=data)
    steps = "; ".join(f"r{t['round']} {t.get('tool', 'check')}: {t.get('summary', t.get('check'))}" for t in result["trace"])
    print(f"\nLIVE {name} | refused={result['refused']} | cost=${result['cost_usd']:.4f} | {steps}\n{result['answer']}\n")
    assert passes(result), result["answer"]
