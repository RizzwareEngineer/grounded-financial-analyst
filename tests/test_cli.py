import json
import sys
import types

import pytest

from analyst import __main__ as cli
from analyst import llm


def stub(monkeypatch, name, **fns):
    mod = types.ModuleType(f"analyst.{name}")
    for k, v in fns.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, f"analyst.{name}", mod)
    monkeypatch.setattr(sys.modules["analyst"], name, mod, raising=False)


def reply(answer="ok 5", cited=("A.pdf",), refused=False):
    return {"answer": answer, "cited": list(cited), "refused": refused, "trace": ["round 1", "round 2"], "cost_usd": 0.01}


def test_ingest(monkeypatch, capsys):
    seen = {}

    def run(docs_dir, only=None, workers=4, model=None):
        seen.update(docs_dir=docs_dir, only=only, workers=workers)
        return {"docs": 1, "pages": 2, "facts": 3, "chunks": 4, "failed_pages": ["x.pdf p3"], "cost_usd": 0.5}

    stub(monkeypatch, "ingest", run=run)
    cli.main(["ingest", "d", "--only", "APL", "--workers", "2"])
    out = capsys.readouterr().out
    assert seen == {"docs_dir": "d", "only": "APL", "workers": 2}
    assert "pages 2" in out and "x.pdf p3" in out and "$0.5000" in out


def test_ask(monkeypatch, capsys):
    stub(monkeypatch, "agent", ask=lambda q, debug=False: reply("The answer", ("A.pdf", "B.pdf")))
    cli.main(["ask", "q"])
    out = capsys.readouterr().out
    assert "The answer" in out and "Sources: A.pdf, B.pdf" in out and "trace" not in out


def test_ask_refusal_has_no_sources(monkeypatch, capsys):
    stub(monkeypatch, "agent", ask=lambda q, debug=False: reply("not in the corpus", (), True))
    cli.main(["ask", "q"])
    assert "Sources:" not in capsys.readouterr().out


def test_ask_on_a_test_question_shows_the_expected_answer_and_the_check(monkeypatch, capsys, tmp_path):
    row = {"id": "T1", "category": "lookup", "question": "What was revenue?", "must_include": [["9,384", "9.4"]],
           "must_cite": ["A.pdf"], "should_refuse": "no", "expected": "Revenue was INR9,384m.", "holdout": False}
    golden = tmp_path / "golden.jsonl"
    golden.write_text(json.dumps(row))
    monkeypatch.setattr(cli, "GOLDEN", golden)
    stub(monkeypatch, "agent", ask=lambda q, debug=False: reply("Revenue was INR 9,384 m. [A.pdf]", ("A.pdf",)))
    cli.main(["ask", "what was  revenue"])  # matched whatever the case, spacing or question mark
    out = capsys.readouterr().out
    assert "1. Revenue was INR 9,384 m." in out and "source: A.pdf" in out
    assert "EXPECTED (test case T1)" in out and "Revenue was INR9,384m." in out and "CHECK: PASS" in out
    stub(monkeypatch, "agent", ask=lambda q, debug=False: reply("Revenue was INR 1 m. [A.pdf]", ("A.pdf",)))
    cli.main(["ask", "What was revenue?"])
    assert "CHECK: FAIL (missing 9,384 / 9.4)" in capsys.readouterr().out
    cli.main(["ask", "Some other question"])
    assert "EXPECTED" not in capsys.readouterr().out


def test_ask_debug(monkeypatch, capsys):
    seen = []
    stub(monkeypatch, "agent", ask=lambda q, debug=False: seen.append(debug) or reply())
    cli.main(["ask", "q", "--debug"])
    assert seen == [True] and "trace" not in capsys.readouterr().out  # the agent prints the steps itself, once


@pytest.fixture
def evalenv(monkeypatch, tmp_path):
    rows = [{"id": "S1", "category": "lookup", "question": "q1", "must_include": [["5"]], "must_cite": ["A.pdf"],
             "should_refuse": "no", "holdout": False},
            {"id": "S2", "category": "why", "question": "q2", "must_include": [["7"]], "must_cite": [],
             "should_refuse": "no", "holdout": False},
            {"id": "H1", "category": "why", "question": "q3", "must_include": [], "must_cite": [],
             "should_refuse": "no", "holdout": True}]
    golden = tmp_path / "golden.jsonl"
    golden.write_text("\n".join(json.dumps(r) for r in rows))
    monkeypatch.setattr(cli, "GOLDEN", golden)
    monkeypatch.setattr(cli, "LATEST", tmp_path / "results" / "latest.json")
    monkeypatch.setattr(llm, "spent", lambda purpose=None: 1.5)
    monkeypatch.setattr(llm, "guard", lambda: None)
    return tmp_path


def test_eval_scores_writes_and_reruns_failed(monkeypatch, evalenv, capsys):
    asked = []

    def ask(q, debug=False):
        asked.append(q)
        return reply("ok 5") if q == "q1" else reply("nope")

    stub(monkeypatch, "agent", ask=ask)
    cli.main(["eval"])
    out = capsys.readouterr().out
    assert "PASS S1" in out and "FAIL S2" in out and "missing 7" in out and "overall: 1/2" in out
    assert "project total $1.5000" in out
    saved = json.loads(cli.LATEST.read_text())
    assert saved["S1"]["passed"] and not saved["S2"]["passed"] and saved["S2"]["category"] == "why"
    assert "H1" not in saved

    asked.clear()
    cli.main(["eval", "--failed"])
    assert asked == ["q2"]
    assert set(json.loads(cli.LATEST.read_text())) == {"S1", "S2"}


def test_eval_tiers(monkeypatch, evalenv, capsys):
    asked = []
    stub(monkeypatch, "agent", ask=lambda q, debug=False: asked.append(q) or reply("ok 5 7"))
    cli.main(["eval", "--tier", "holdout"])
    assert asked == ["q3"]
    asked.clear()
    cli.main(["eval", "--tier", "all", "--workers", "1"])
    assert sorted(asked) == ["q1", "q2", "q3"]


def test_eval_exception_is_a_failure(monkeypatch, evalenv, capsys):
    def ask(q, debug=False):
        if q == "q1":
            raise RuntimeError("boom")
        return reply("ok 7")

    stub(monkeypatch, "agent", ask=ask)
    cli.main(["eval"])
    out = capsys.readouterr().out
    assert "FAIL S1 error: boom" in out and "PASS S2" in out
    assert json.loads(cli.LATEST.read_text())["S1"]["reasons"] == ["error: boom"]


def test_eval_budget_stop_skips_and_keeps_old_results(monkeypatch, evalenv, capsys):
    stub(monkeypatch, "agent", ask=lambda q, debug=False: reply("ok 5 7"))
    cli.main(["eval"])
    before = cli.LATEST.read_text()

    def guard():
        raise llm.BudgetExceeded("Spent $25.00 of the $25.00 budget")

    monkeypatch.setattr(llm, "guard", guard)
    capsys.readouterr()
    cli.main(["eval", "--tier", "all"])
    out = capsys.readouterr().out
    assert cli.LATEST.read_text() == before  # earlier passes are not overwritten by a budget stop
    assert "SKIP H1" in out and "budget" in out and "overall: 0/0" in out and "3 skipped" in out


def test_eval_failed_includes_never_run(monkeypatch, evalenv, capsys):
    asked = []
    stub(monkeypatch, "agent", ask=lambda q, debug=False: asked.append(q) or reply("ok 5 7"))
    cli.main(["eval", "--failed"])  # no previous results: nothing has passed yet, so run the tier
    assert sorted(asked) == ["q1", "q2"]
    asked.clear()
    cli.main(["eval", "--failed"])
    assert asked == [] and "overall: 0/0" in capsys.readouterr().out


def test_eval_empty_answer(monkeypatch, evalenv, capsys):
    stub(monkeypatch, "agent", ask=lambda q, debug=False: reply("", ()))
    cli.main(["eval"])
    out = capsys.readouterr().out
    assert "FAIL S1" in out and "FAIL S2" in out and "overall: 0/2" in out
