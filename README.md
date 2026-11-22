# Grounded Financial Analyst

A command-line analyst that answers questions over a folder of broker research PDFs. Every claim cites the report it came from, and when the reports don't contain the answer it says so instead of guessing.

## System design

![System design](design/system-design.png)

[Open the diagram in Excalidraw](https://excalidraw.com/#json=aqtb8Bf5-akUSDwmIPj4F,3NEGRYvBDSxzfbNC-KFvWQ) (use a private window if you already have a drawing open there), or load [design/system-design.excalidraw](design/system-design.excalidraw).

## Walkthrough

### Ingest (once)

1. **Read.** PyMuPDF cuts each report into pages and pulls the raw text; pages after the broker's disclosure heading are skipped. Each page goes to Claude Haiku 5.5 as a one-page PDF and comes back as structured JSON: text blocks, tables as columns and rows of values, and chart descriptions. Page 1 also yields the report card (company, sector, rating, price, target price).
2. **Check, in code.** Every value must be printed in that page's PyMuPDF text, every row must have one value per column, and where a table has four quarters and a full year they must add up (a misplaced column fails the page).
3. **Store.** Each table cell becomes one row in SQLite, with its company, row label, period, unit, actual-or-forecast flag, document and page. Text is cut into chunks of at most 160 words, embedded with all-MiniLM-L6-v2 into Chroma, and indexed for BM25 keyword search with SQLite FTS5.

**If something fails:** a page that fails the checks is read again by Haiku, then by Claude Sonnet 5.5. If it still fails it is listed as failed in the run summary, and a row with the wrong number of values is not stored.

**When the reports change:** run ingest again. Page results are cached by the file's content, so a new report is read, an edited one is read again, an unchanged one costs nothing, and a deleted one is removed from the stores. Each report's rows are replaced as a unit, so nothing stale is left behind.

### Query (every question)

1. **Loop.** The question goes to Claude Opus 5.5 with five lookup tools:
   - `list_reports()`: the catalog of reports.
   - `list_labels(company)`: the table rows one report has.
   - `query_numbers(sql)`: one read-only `SELECT` over the SQLite numbers.
   - `search_text(query, companies)`: Chroma vector search plus FTS5 keyword search, merged, one search per company.
   - `read_page(company, page)`: everything on one page of a report. Page 1 is the broker's own summary, so "why" questions start there and don't depend on search wording.

   Each round Claude replies with a tool call. A lookup tool means it needs more; `submit_answer` means it has enough. Code runs the lookup, adds the result to the conversation and asks again, for at most 8 rounds.
2. **Submit.** `submit_answer` takes a list of statements, each with the reports it cites. Free text is never accepted as the answer. The first submission is always sent back once with one question: is any part of the question, or a reason the report's summary gives, missing?
3. **Check, in code.** Each cited report must be one a tool returned in this run, and every number must appear in a tool result. Rounding and million/billion scaling are allowed; anything calculated has to be computed in SQL so that it shows up in a result.

**If something fails:**
- **A lookup finds nothing:** the tool returns the valid company names and a hint, so Claude retries with `list_labels` or different wording.
- **A statement fails the check:** it goes back to Claude with the reason, in the same single send-back. What still fails is dropped.
- **Nothing survives, or the tools never found support:** the answer is "Not in the corpus".

### Eval (after every change)

`evals/golden.jsonl` holds 47 questions, each with the numbers an answer must contain, the reports it must cite, and whether it should refuse. `python -m analyst eval` runs each question through the CLI and plain Python scores it; no model grades the answers. The expected values were checked against the PDF text, and 15 questions are held out from tuning.

| Kind of question | Count | Example |
|---|---|---|
| Lookup: one or a few cells of one report | 8 | Revenue against the broker's estimate |
| Derivation | 5 | How a target price is arrived at |
| Multi-cell: several columns of one row, or one cell across reports in different units | 3 | A company's revenue in each quarter of a year |
| Ranking and corpus-wide, across two to eight reports | 7 | Which stocks have a target price below the current price |
| Sector | 4 | How did healthcare do last quarter |
| Why and outlook | 6 | Why a margin fell |
| The report contradicts itself | 4 | Two different figures for one line |
| Should refuse | 6 | A company that is not in the corpus |
| Wrong premise or half answerable | 4 | "Why was it downgraded?" when it wasn't |

12 of the 47 need two or more reports. The brief's six samples are among them.

**If something fails:** the scorecard prints the reason per question (a missing number, a missing citation, a wrong refusal), and `--failed` re-runs only those.

### One question, end to end

**Question:** "Why did Escorts Kubota's gross margin decline in 1QFY27 despite 28% revenue growth, and what is the margin outlook?"

It needs numbers from tables and reasons from the commentary, so it touches both stores. This is the trace of a real run (`ask "..." --debug`), which cost $0.15:

| Round | Claude calls | What comes back |
|---|---|---|
| 1 | `read_page("Escorts Kubota", 1)` | The broker's page-1 summary of the quarter |
| 1 | `query_numbers`: revenue, growth and margin rows for 1QFY26, 4QFY26, 1QFY27, FY27 and FY28 | 71 rows, each with document and page |
| 2 | `search_text("margin outlook input cost inflation price hike commodity steel")` | Chunks from the outlook and management commentary |
| 3 | `submit_answer` | Sent back once for the completeness review |
| 4 | `submit_answer` again | Accepted: every statement cites `MotilalOswal_ESCORTS_5765676.pdf` and every number traces to a tool result |

The answer, shortened to four of its eight statements:

> Escorts Kubota's 1QFY27 standalone revenue was INR31,789m, up 28% YoY and 1.7% above the broker's estimate of INR31,269m. [MotilalOswal_ESCORTS_5765676.pdf]
> Despite that growth, gross margin fell about 370bp YoY to 27.2% (from 30.9% in 1QFY26). The report says this was mainly due to raw material cost inflation. [MotilalOswal_ESCORTS_5765676.pdf]
> The Construction Equipment margin came in at 5.4%, against an estimate of 7.5%. Another page of the same report gives the segment's EBIT margin as 5.8%. [MotilalOswal_ESCORTS_5765676.pdf]
> Outlook: the broker expects margins to stay under pressure at least through 2Q and then recover gradually as input costs ease in 2H. [MotilalOswal_ESCORTS_5765676.pdf]

It states that the revenue figure is standalone, and it reports the report's own inconsistency (5.4% against 5.8%) instead of picking one.

## Run it

Needs Python 3.13, [uv](https://docs.astral.sh/uv/) and an Anthropic API key. The PDFs are not in this repository because they are the broker's copyrighted reports; put them in `docs/`.

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -r requirements.txt
cp .env.example .env                                   # then paste your key into .env
.venv/bin/python -m analyst ingest docs/               # once; cached, so re-runs are free
.venv/bin/python -m analyst ask "What rating and target price does the broker assign to NALCO?" --debug
.venv/bin/python -m analyst eval --tier samples        # the six sample questions
.venv/bin/python -m pytest -q
```

Every command prints what it spent. A budget cap (`ANALYST_BUDGET_USD`, default 25) stops paid calls when it is reached.

## Review Q&A

<details>
<summary><b>Why not put the tables in the vector database?</b></summary>

Vector search matches meaning, and a number has no meaning to match on. Exact cells, questions that span several columns, and rankings need an exact lookup, so numbers go in SQL and only narrative goes through search.

</details>

<details>
<summary><b>How does the model know what SQL to write, and is that safe?</b></summary>

Its instructions carry the table's columns, the company names with their aliases, and the standard metric names; for any other row it calls `list_labels` first. The query runs on a read-only connection, as a single `SELECT`, capped at 200 rows.

</details>

<details>
<summary><b>How do you know the tables were read correctly?</b></summary>

Each value must be printed on its page, each row must have one value per column, and four quarters must add up to the full year. Across the 17 reports, 17,612 of 17,651 values are printed on their page; the other 39 are one table that is an image, and that page is flagged.

</details>

<details>
<summary><b>Why a vision model for ingestion?</b></summary>

PDF text extraction loses which number sits under which column and gets nothing from charts. Reading the page as an image keeps the layout.

</details>

<details>
<summary><b>How does the loop know it needs another tool call, and when does it stop?</b></summary>

Each round the model sees the question and every result so far, and replies with either a lookup tool or `submit_answer`; choosing a lookup is what "needs more" means. It stops at `submit_answer` or after 8 rounds.

</details>

<details>
<summary><b>What stops it answering too early with a thin answer?</b></summary>

For "why" questions it reads the report's first page before searching, and its first answer is always sent back once for a completeness check. The eval lists the numbers a complete answer must contain, so a thin answer fails there; retrying until the answer stops improving would need a second model to judge each round.

</details>

<details>
<summary><b>How is grounding enforced?</b></summary>

The model submits statements with sources, and code keeps a statement only if its source is a report a tool returned and every number in it appears in a tool result. Failures go back once, then are dropped.

</details>

<details>
<summary><b>When does it refuse?</b></summary>

When the company is not in the catalog, when the tools find nothing after retries, or when no statement survives the check. A half-answerable question gets the supported half plus a line saying what is missing.

</details>

<details>
<summary><b>Why these models and stores?</b></summary>

Claude Opus 5.5 runs the loop, where judgment matters; Claude Haiku 5.5 reads pages because it matched Opus on every value at about one twenty-sixth of the cost. SQLite, Chroma and MiniLM all run locally with no server, which fits a corpus of this size.

</details>

<details>
<summary><b>Why not fine-tune, and why no LangChain?</b></summary>

Knowledge baked into weights cannot be cited or reliably refused, and six sample questions are a test set, not training data. LangChain would wrap the same Claude, Chroma and SQLite calls and sit between the code and the raw tool results the check needs.

</details>

<details>
<summary><b>What happens when a report is added, edited or removed?</b></summary>

Run ingest again. The page cache is keyed by each file's content, so only new or edited reports are read (about $0.02 each), and a report whose file is gone is deleted from the stores; the catalog is rebuilt for every question, so the change is visible at once.

</details>

<details>
<summary><b>What if the sources change constantly, like live Google Drive files?</b></summary>

Give each document a version stamp (a content hash or the Drive revision id) and re-ingest only what changed, triggered by a schedule or Drive's change notifications. Replacing a document's rows in one transaction is simpler and safer than tracking differences; keeping old versions only matters if users ask what changed between them.

</details>

<details>
<summary><b>How would this scale to thousands of reports?</b></summary>

The stores and tools stay the same. The catalog moves out of the prompt into a lookup tool, Chroma is swapped for a hosted vector store, and ingestion is already per page and parallel.

</details>

## Limits

- A general-knowledge sentence attached to a real citation can pass the check; the refusal questions in the eval are what catch it.
- Numbers are checked against everything the tools returned in the run, not per statement, so a number from one result can vouch for a statement about another.
- A value misread into a neighbouring cell is only caught where a total exists to cross-check it.
- Chart values are described, not measured, and are marked approximate.

## Results

| | |
|---|---|
| Ingest | 17 reports, 147 pages, 17,651 numbers, 580 text chunks. 146 pages pass every check; one page, a table that is an image, is flagged |
| Tuning questions | 32 of 32 pass |
| Held-out questions, run once | 14 of 15 pass |
| Cost | About $0.07 a question on Claude Opus 5.5 (highest $0.19); about $0.0025 a page to ingest on Claude Haiku 5.5 |
| Unit tests | 274 pass |

The held-out miss asked for a five-year average P/E. The report only has a ten-year average, printed on a chart; the answer said the five-year figure isn't given and offered the ten-year one marked approximate, where the test expected a plain refusal.
