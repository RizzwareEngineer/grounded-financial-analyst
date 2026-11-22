# Grounded Financial Analyst

A command-line analyst that answers questions over a folder of broker research PDFs. Every claim cites the report it came from, and when the reports don't contain the answer it says so instead of guessing.

## System design

![System design](design/system-design.png)

[Open the diagram in Excalidraw](https://excalidraw.com/#json=_Be9Rk8O-ReE8E9UG0v2w,hqwJXy8IbAgcRC-iVKd19Q) (use a private window if you already have a drawing open there), or load [design/system-design.excalidraw](design/system-design.excalidraw).

## Walkthrough

The numbers match the diagram. There are three passes: ingest runs once, query runs for every question, and eval runs after every change.

<details>
<summary><b>Ingest: blocks 1 to 6 (runs once)</b></summary>

| # | Block | What it does |
|---|---|---|
| 1 | 17 PDFs | The broker reports we were given. Nothing else is ever used as a source. |
| 2 | Read each page | A PDF only stores letters at positions, so a table comes out as loose numbers and a chart as nothing. A vision model (Claude Haiku 5.5) looks at each page the way a person does and types it out: paragraphs as text, tables as rows and columns, charts as a short description. Its whole job is to turn the page into readable text. |
| 3 | Check numbers | We don't trust that typing. PyMuPDF pulls the page's raw characters, and code checks that every number the model wrote is really printed on that page, that every table row has a value for every column, and that four quarters add up to the year. |
| 4 | Split | Code cuts each page two ways. Every table cell becomes one row of numbers. Every paragraph becomes a text chunk of at most 160 words. For each table and chart the model also wrote a one-line description, the search text, so it can be found later. |
| 5 | Embed + load | The text chunks go through MiniLM, a small local model that turns text into a list of numbers so that similar meanings land close together, and are saved in Chroma. The rows of numbers are saved in SQLite. |
| 6 | Stores | What we end up with: a catalog with one row per report (company, sector, rating, target price); a numbers table with one row per number (17,651 rows); and 580 text chunks that can be searched by meaning (Chroma) and by keyword (SQLite FTS5, BM25). |

**If something fails:** a page that fails block 3 is read again, then read by a stronger model (Claude Sonnet 5.5), then listed as failed. One page of 147 is flagged: a table that is an image.

**When the reports change:** run ingest again. Only new or edited reports are read, and a deleted report is removed from the stores.

</details>

<details>
<summary><b>Query: blocks 7 to 11 (every question)</b></summary>

| # | Block | What it does |
|---|---|---|
| 7 | User (CLI) | Someone types a question into the command line. |
| 8 | Agent loop | The question goes to Claude Opus 5.5 together with the tools. Each round Claude does one of two things: it asks for a tool ("I need more") or it submits an answer ("I have enough"). That choice is check 1. Code runs the tool, hands the result back and asks again, at most eight times. |
| 9 | Tools | Five ways to look something up, each reading one store:<br>`list_reports`: which reports do we have?<br>`list_labels`: which rows do this report's tables have?<br>`query_numbers`: fetch exact numbers with a SQL query.<br>`search_text`: find paragraphs about a topic.<br>`read_page`: hand over one whole page of a report. Page 1 is the broker's own summary, so a "why" question starts by reading it. |
| 10 | Answer check | Claude does not write free text. It submits statements, each with the report it cites. Code checks that every cited report was really returned by a tool and that every number appears in a tool result. The first answer is always sent back once with "is anything missing?". This is check 2. |
| 11 | Answer | What survives is printed with its sources. If nothing survives, or the tools found nothing, it prints "Not in the corpus". |

**If something fails:**
- **A lookup finds nothing:** the tool replies with the valid company names and a hint, so Claude tries `list_labels` or different search words.
- **A statement fails check 2:** it goes back to Claude once with the reason. What still fails is removed.
- **The question can't be answered from the reports:** the answer is "Not in the corpus". A half-answerable question gets the half that is supported, plus a line saying what is missing.

</details>

<details>
<summary><b>Eval: blocks 12 to 15 (after every change)</b></summary>

| # | Block | What it does |
|---|---|---|
| 12 | golden.jsonl | 47 questions, each with the numbers a right answer must contain, the reports it must cite, and whether it should be refused. The expected values were checked against the PDF text. |
| 13 | Run the CLI | Each question is run through the same command a user would type. |
| 14 | Code checks | Plain code compares each answer with the expected numbers, sources and refusals. No model grades the answers. |
| 15 | Scorecard | Pass or fail per question, with the reason. Today: 32 of 32 tuning questions and 14 of 15 held-out questions. |

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

</details>

<details>
<summary><b>How the model finds the right number</b></summary>

The reports' tables all have different shapes, so they are not stored as tables. Every printed cell becomes one row in a single table, `facts`, and that table has the same columns for every report:

| Column | What it holds | Example |
|---|---|---|
| `company` | The report's company | GSK Pharma |
| `doc`, `page` | Where the number is printed; this is the citation | `MotilalOswal_GLAXO_5765685.pdf`, 2 |
| `source_table` | The printed table's title | Qtr Perf. (Consol.) |
| `row_label` | The row's name as printed | Net Sales |
| `metric` | A standard name for ten common lines (revenue, EBITDA, PAT, margins, EPS); empty for every other row | revenue |
| `period` | The printed column, as a fiscal period | 1QFY27 |
| `kind` | actual, forecast, broker_estimate or variance_pct | actual |
| `value`, `unit` | The number as printed | 9384, INR m |
| `value_inr_m` | Money totals converted to INR millions | 9384 |
| `basis` | consolidated, standalone, segment or subsidiary | consolidated |

So one printed row becomes one `facts` row per column. The "Net Sales" row of GSK Pharma's quarterly table is stored as:

| `row_label` | `period` | `kind` | `value` | `unit` |
|---|---|---|---|---|
| Net Sales | 1QFY27 | actual | 9384 | INR m |
| Net Sales | 1QFY27 | broker_estimate | 8788 | INR m |
| Net Sales | 1QFY27 | variance_pct | 7 | % |
| Net Sales | 2QFY27 | forecast | 11275 | INR m |

The model is told these columns and their allowed values in its instructions. There are two ways it picks a row:

1. **A common line.** It filters on the standard name: `metric = 'revenue' AND period = '1QFY27' AND kind = 'actual'`. It doesn't need to know how each report words the row.
2. **Any other row.** It calls `list_labels(company)` first. That returns one line per printed row of that report (122 lines for GSK Pharma) and no values:

   | `source_table` | `row_label` | `metric` | `unit` | `basis` | `periods` |
   |---|---|---|---|---|---|
   | Qtr Perf. (Consol.) | Net Sales | revenue | INR m | consolidated | 1QFY26, 2QFY26, … FY27 |
   | Qtr Perf. (Consol.) | Net Sales: YoY Change (%) | revenue_growth_yoy_pct | % | consolidated | 1QFY26, 2QFY26, … FY27 |
   | Qtr Perf. (Consol.) | EBITDA: Margins (%) | ebitda_margin_pct | % | consolidated | 1QFY26, 2QFY26, … FY27 |

   The model reads the labels, picks the one that matches the question, checks that the period it wants is listed, and then queries `facts` with that exact `row_label` and `period`.

</details>

<details>
<summary><b>One question, end to end</b></summary>

**Question:** "Why did Escorts Kubota's gross margin decline in 1QFY27 despite 28% revenue growth, and what is the margin outlook?"

It needs numbers from tables and reasons from the commentary, so it uses the agent loop (block 8), three of the tools (block 9) and both checks. This is the trace of a real run (`ask "..." --debug`), which cost $0.15:

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

</details>

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
