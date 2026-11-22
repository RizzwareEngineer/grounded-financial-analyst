import argparse
import json
import re
import shutil
import sys
import textwrap
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "evals" / "golden.jsonl"
LATEST = ROOT / "evals" / "results" / "latest.json"


def cmd_ingest(args):
    from . import ingest
    s = ingest.run(args.docs_dir, only=args.only, workers=args.workers, model=args.model)
    print(f"docs {s['docs']}, pages {s['pages']}, facts {s['facts']}, chunks {s['chunks']}")
    for page in s["failed_pages"]:
        print(f"FAILED page: {page}")
    print(f"cost ${s['cost_usd']:.4f}")


def _bold(s):
    return f"\033[1m{s}\033[0m" if sys.stdout.isatty() else s


def _para(text, first="  ", rest="  "):
    width = max(60, min(shutil.get_terminal_size().columns, 110))
    return textwrap.fill(text, width=width, initial_indent=first, subsequent_indent=rest)


def _test_case(question):
    """The golden row with this question, if there is one: asking a test question also shows what was expected."""
    def norm(s):
        return " ".join(s.lower().split()).rstrip(" ?.")
    rows = [json.loads(line) for line in GOLDEN.read_text().splitlines() if line.strip()] if GOLDEN.exists() else []
    return next((row for row in rows if norm(row["question"]) == norm(question)), None)


def cmd_ask(args):
    from . import agent, evalcheck
    r = agent.ask(args.question, debug=args.debug)  # with --debug the agent prints each step as it happens
    print("\n" + _bold("ANSWER"))
    n = 0
    for line in r["answer"].splitlines():
        claim = re.fullmatch(r"(.*?)((?: \[[^\]]+\])+)", line)  # a statement ends with its sources: text [a.pdf] [b.pdf]
        if not claim:  # the refusal line, or a note
            print(_para(line) + "\n")
            continue
        n += 1
        print(_para(claim[1], f"  {n}. ", "     "))
        print("     source: " + ", ".join(re.findall(r"\[([^\]]+)\]", claim[2])) + "\n")
    if not r["refused"]:
        print("Sources: " + ", ".join(r["cited"]))
    row = _test_case(args.question)
    if row:  # the same check `eval` runs: expected numbers or words, expected sources, refuse or not
        passed, reasons = evalcheck.check(row, r["answer"], r["cited"], r["refused"])
        print("\n" + _bold(f"EXPECTED (test case {row['id']})"))
        print(_para(row.get("expected") or "A refusal: the reports do not contain this."))
        needs = [" or ".join(group) for group in row["must_include"]] + [f"a citation of {doc}" for doc in row["must_cite"]]
        print(_para("The check needs: " + ("; ".join(needs) if row["should_refuse"] != "yes" else "a refusal")))
        print(_bold("CHECK: PASS" if passed else "CHECK: FAIL") + ("" if passed else " (" + "; ".join(reasons) + ")"))
    print(f"\ncost ${r['cost_usd']:.4f}")


def cmd_eval(args):
    from . import agent, evalcheck, llm
    rows = [json.loads(line) for line in GOLDEN.read_text().splitlines() if line.strip()]
    old = json.loads(LATEST.read_text()) if LATEST.exists() else {}
    if args.ids:
        want = set(args.ids.split(","))
        rows = [r for r in rows if r["id"] in want]
    elif args.tier == "samples":
        rows = [r for r in rows if r["id"].startswith("S")]
    elif args.tier == "tuning":
        rows = [r for r in rows if not r["holdout"]]
    elif args.tier == "holdout":
        rows = [r for r in rows if r["holdout"]]
    if args.failed:
        rows = [r for r in rows if not old.get(r["id"], {}).get("passed")]  # failed or never run

    def run(row):
        base = {"category": row["category"], "question": row["question"]}
        try:
            llm.guard()
            r = agent.ask(row["question"])
            passed, reasons = evalcheck.check(row, r["answer"], r["cited"], r["refused"])
            return row["id"], {**base, "passed": passed, "reasons": reasons, "answer": r["answer"],
                               "cited": r["cited"], "refused": r["refused"], "cost_usd": r["cost_usd"]}
        except llm.BudgetExceeded as e:
            return row["id"], str(e)  # not a result: keep whatever latest.json already has
        except Exception as e:
            return row["id"], {**base, "passed": False, "reasons": [f"error: {e}"], "answer": "",
                               "cited": [], "refused": False, "cost_usd": 0.0}

    with ThreadPoolExecutor(args.workers) as pool:
        done = dict(pool.map(run, rows))
    results = {i: r for i, r in done.items() if isinstance(r, dict)}
    old.update(results)
    LATEST.parent.mkdir(parents=True, exist_ok=True)
    LATEST.write_text(json.dumps(old, indent=2))

    by_cat = {}
    for row in rows:
        res = done[row["id"]]
        if isinstance(res, str):
            print(f"SKIP {row['id']} {res}")
            continue
        print(f"{'PASS' if res['passed'] else 'FAIL'} {row['id']} {'; '.join(res['reasons'])}")
        p, n = by_cat.get(res["category"], (0, 0))
        by_cat[res["category"]] = (p + res["passed"], n + 1)
    for cat, (p, n) in sorted(by_cat.items()):
        print(f"{cat}: {p}/{n}")
    skipped = len(rows) - len(results)
    print(f"overall: {sum(p for p, _ in by_cat.values())}/{len(results)}" + (f", {skipped} skipped (re-run with --failed)" if skipped else ""))
    print(f"this run ${sum(r['cost_usd'] for r in results.values()):.4f}, project total ${llm.spent():.4f}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="analyst")
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("ingest")
    i.add_argument("docs_dir", nargs="?", default="docs")
    i.add_argument("--only")
    i.add_argument("--workers", type=int, default=4)
    i.add_argument("--model")
    i.set_defaults(fn=cmd_ingest)
    a = sub.add_parser("ask")
    a.add_argument("question")
    a.add_argument("--debug", action="store_true")
    a.set_defaults(fn=cmd_ask)
    e = sub.add_parser("eval")
    e.add_argument("--tier", choices=["samples", "tuning", "holdout", "all"], default="samples")
    e.add_argument("--ids")
    e.add_argument("--failed", action="store_true")
    e.add_argument("--workers", type=int, default=4)
    e.set_defaults(fn=cmd_eval)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
