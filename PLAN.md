# Grounded Financial Analyst: build plan

Numbers match the red badges on the system diagram.

## What gets built

| # | Piece | File | What it does | How we know it works |
|---|---|---|---|---|
| 1 | PDF text | `analyst/pdf.py` | Raw text per page; stops at the disclaimer pages | All 17 reports open; each has a cut-off page; known figures (9,384 in the GSK report, 82,882 in NALCO) are found |
| 2 | Page reader | `analyst/ingest.py` | Vision LLM turns one page into markdown plus table rows as JSON; page 1 also yields the report card | APL first: card reads Buy / 1,942 / 2,240; 1QFY27 EBITDA row is 4,113 |
| 3 | Number check | `analyst/pdf.py` | Every number the model reported must be printed on that page | At least 99% of rows pass; failures are listed and the page is re-run once |
| 4 | Split and load | `analyst/ingest.py` | Cuts markdown into chunks at headings, tables, exhibits; writes search text; embeds; loads rows | Every report has rows and chunks; GSK 2QFY27 is stored as a forecast; Cello has no 1QFY27 actuals |
| 5 | Stores | `analyst/store.py` | SQLite: `reports`, `facts`, `chunks` + FTS5 (BM25). Chroma: vectors (all-MiniLM-L6-v2) | Round-trip test on made-up rows; forecast rule tested on 6 period pairs |
| 6 | Tools | `analyst/tools.py` | `list_reports`, `list_labels`, `query_numbers`, `search_text` | SQL for S2 returns 9,384 and 8,788; search for S3 returns the raw-material chunk; a `DELETE` is rejected |
| 7 | Agent loop | `analyst/agent.py` | Model picks a tool, reads the result, retries or answers; cap of 8 rounds | The six sample questions pass; `--debug` shows each round |
| 8 | Answer check | `analyst/agent.py` | Every number traces to a tool result; only documents a tool returned can be cited; otherwise "not in the corpus" | An answer with an invented number is rejected; an uncited sentence is dropped |
| 9 | CLI | `analyst/__main__.py` | `ingest docs/`, `ask "..." [--debug]`, `eval` | Each command runs from a clean checkout |
| 10 | Eval | `analyst/evalcheck.py`, `evals/golden.jsonl` | Runs the 44 questions, checks numbers, sources, refusals, prints pass/fail | Checker tested on one known-good and one known-bad answer per category |

Fixture needed for the check on piece 2: `tests/cards.json`, the rating, CMP and target price for all 17 reports.

## Order and what runs in parallel

| Phase | Work | Parallel? | Gate before moving on |
|---|---|---|---|
| 0 | Environment, 17 PDFs, test set | Done | 17 valid PDFs; libraries import |
| 1 | Pieces 1, 5, 6, 10 (none need the LLM) | All four in parallel; separate files | Unit checks pass on made-up data |
| 2 | Pieces 2 and 3 on the APL report only | One after the other | APL card and table spot-checks pass |
| 3 | Pieces 2, 3, 4 on all 17 reports | Page calls run in parallel | 17 cards match the fixture; number check at 99% or better; S2 and S6 answerable by SQL alone |
| 4 | Pieces 7, 8, 9 | 8 and 9 in parallel with 7 | Six sample questions pass |
| 5 | Full eval, fixes, README, demo list | Eval runs in parallel per question | 29 tuning questions reviewed; 15 held-out questions run once |

Phase 2 exists so a bad page-reading prompt is caught on one report, not after 125 page calls.

## Interfaces fixed up front (so parallel work fits together)

These names, parameters and return shapes are frozen. Fix a bug inside one only if you own the file.

- `pdf.content_pages(path) -> [(page_number, text)]`, `pdf.numbers(text) -> set[float]`, `pdf.on_page(value, page_text) -> bool`
- `store.connect(data=DATA, readonly=False)`, `store.collection(data=DATA)`; the tables are in `store.SCHEMA`
- `store.add_report(conn, card)`, `store.add_facts(conn, company, doc, reported_period, facts)`, `store.add_chunks(conn, coll, chunks)`, `store.clear_doc(conn, coll, doc)`
- A report card: the keys in `store.REPORT_COLS`. `aliases` is a comma-separated string; `reported_period` is like `1QFY27`, or `FY26` for a report with no quarter results
- A fact passed to `add_facts`: `page, source_table, row_label, metric, period, kind, value, unit, basis`. `metric` is one of `store.METRICS` or None; `kind` one of `store.KINDS`; `basis` one of `store.BASES` or None; money totals use a unit from `store.MONEY_UNITS`. `add_facts` sets forecast-by-date and fills `value_inr_m`
- A chunk: `id, company, doc, page, section, kind, content, search_text`; `kind` is `text`, `table` or `chart`
- `tools.list_reports(conn)`, `tools.list_labels(conn, company)`, `tools.query_numbers(sql, data=DATA)`, `tools.search_text(conn, coll, query, companies=None, k=6)`; every returned row carries `doc` and `page`
- `llm.client()`, `llm.record(model, usage, purpose) -> usd`, `llm.spent(purpose=None)`, `llm.guard()`; every Claude call is recorded through these
- `ingest.run(docs_dir="docs", only=None, workers=4, model=None, data=store.DATA) -> {docs, pages, facts, chunks, failed_pages, cost_usd}`; re-running is idempotent and cached pages cost nothing
- `agent.ask(question, data=store.DATA, debug=False) -> {answer, cited, refused, trace, cost_usd}`; `cited` is a list of document file names
- `evalcheck.check(row, answer, cited, refused) -> (passed, reasons)`

## Cut on purpose

- Reranker: search is already filtered to one company.
- Chart reading: stretch goal; exhibits get a title-only chunk.
- LLM judge: "why" answers are read by hand against `expected`.
- Target-price arithmetic check: stretch goal, after the six samples pass.

## Review findings that change the build

| # | Risk | Where it is handled |
|---|---|---|
| R1 | A table value read into the wrong column is exact but wrong, and "the number is on the page" cannot catch it | Piece 3: four quarters must sum to the full year; table figure must agree with the narrative figure for the same line |
| R2 | The same sub-row label ("YoY Change (%)") appears under several lines in one table | Piece 2: sub-rows are stored as "Gross Sales: YoY Change (%)"; a row gets a standard metric name only when the label is unambiguous; basis is always filled |
| R3 | Reports mix INR m and INR b, and four tables carry a wrong unit label | Piece 4: a `value_inr_m` column computed in code; a 1,000x gap against the narrative overrides the label |
| R4 | Rounded or derived numbers ("~7% beat", "INR9.4b") will not literally match a tool result, so a strict check refuses good answers | Piece 8: derived numbers must come from a SQL query; matching allows rounding and m/b scaling; a failing sentence is dropped, the answer is not refused |
| R5 | The embedding model reads only the first 256 tokens of a chunk | Piece 4: chunks capped at about 200 words; BM25 covers the full text |
| R6 | A new key may have low rate limits, and the credit balance is unknown | Piece 2: 4 calls at a time, every page result cached on disk, cost printed after APL before the full run |

## Build agents

Each builder owns its files and writes its own tests. A separate checker, which wrote none of the code, runs the tests and tries to break the piece before the gate.

| Wave | Agents (in parallel) | Spends on Claude? |
|---|---|---|
| 1 | store + tests; pdf + tests on the real PDFs; tools + tests; eval checker + tests | No |
| 1 check | One checker over all four | No |
| 2 | Page reader, on APL only; reports cost per page | Yes, small |
| 3 | Full ingest (a script run, not an agent); meanwhile: agent loop; answer check + CLI | Ingest only |
| 3 check | One checker: 17 cards, R1 and R3 checks, S2 and S6 by SQL alone | No |
| 4 | Six samples, then the 29 tuning questions, fixes, README | Yes |

## Cost limits

- Free checks first: unit tests, SQL-only and retrieval-only checks use no model.
- Every page result and every answer is cached on disk; only failures are re-run.
- Paid eval tiers: 6 samples, then 29 tuning questions, then the 15 held-out questions once.
- Running cost printed by every command from the API's token counts; stop at the agreed cap.
- Prompt caching on the agent's fixed instructions; 8-round cap; search results trimmed.

## Test set status

37 of the 38 answerable questions have every required value printed in a report they cite (checked in code against the PDF text). The six refusal questions cannot be checked that way; HDFC and Hindalco appear in none of the 17 reports.
