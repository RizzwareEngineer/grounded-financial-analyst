# Grounded Financial Analyst

A command-line analyst that answers questions over a folder of broker research PDFs. Every claim cites the report it came from, and when the reports don't contain the answer it says so instead of guessing.

## System design

![System design](design/system-design.png)

[Open the diagram in Excalidraw](https://excalidraw.com/#json=mE-jatDfdgjCbwn4h87bh,NbCEeHEwE3wmJM3fPeC9XA) (use a private window if you already have a drawing open there), or load [design/system-design.excalidraw](design/system-design.excalidraw).

## Walkthrough

The numbers match the diagram. There are three passes: ingest runs once, query runs for every question, and eval runs after every change.

<details>
<summary><b>Ingest: blocks 1 to 6 (runs once)</b></summary>

| # | Block | What it does |
|---|---|---|
| 1 | 17 PDFs | The broker reports we were given, one per company. Nothing else is ever used as a source. |
| 2 | Read each page | A PDF only stores letters at positions, so a table or a chart comes out as loose numbers and a picture as nothing. A vision model (Claude Haiku 5.5) looks at each of the 147 content pages the way a person does and types it out: paragraphs as text, tables as rows and columns, charts as a short description. Its job is to turn the page into readable text. It also gives common rows such as revenue a standard name, and takes the report's rating and target price from page 1. |
| 3 | Check numbers | We don't trust that typing. PyMuPDF pulls the page's raw characters, and code checks that every table value the model typed is printed on that page, that every row has one cell per column, and that quarters add up to the year, which catches a column that slipped. |
| 4 | Split | Code cuts each page two ways. Every table cell becomes one row of the numbers table, `facts`: one number plus its labels. Paragraphs are packed into text chunks of at most 160 words; each table and each chart description is a chunk too. Every chunk gets a search text: a search matches on it and returns the chunk. For a table or chart it is the line the vision model wrote in block 2 (plus the chart's title or the table's row names); for a paragraph, a header built by code (company, report type, section) and the paragraph. No second model is involved. |
| 5 | Embed + load | MiniLM, a small local model, embeds each chunk's search text: it turns it into a vector, a list of numbers placed so that similar meanings sit close together. Chroma, a vector database, stores the vectors. The `facts` rows are never embedded: they go into SQLite, with the catalog and a keyword index of each chunk and its search text. |
| 6 | Stores | What we end up with (top right of the diagram): a catalog with one row per report (company, sector, rating, target price); the numbers table, `facts`, one table for all 17 reports with one row per printed number (17,651 rows, each with its report, page, table, row name, period, value and unit); and 580 chunks of text, tables and charts, searchable by meaning (Chroma) and by keyword (SQLite FTS5, BM25). |

**If something fails:** a page that fails block 3 is read again, then read by a stronger model (Claude Sonnet 5.5), then reported as failed. One page of 147 fails: a table that is a picture, with no printed text to check against. Its 39 values are stored unverified.

**When the reports change:** run ingest again. Only new or edited reports are read, and a deleted report is removed from the stores.

</details>

<details>
<summary><b>Query: blocks 7 to 11 (every question)</b></summary>

| # | Block | What it does |
|---|---|---|
| 7 | User (CLI) | Someone types a question into the command line. |
| 8 | Agent loop | The question goes to Claude Opus 5.5 with a description of each tool. Each round it does one of two things: asks for one or more tools ("I need more") or submits an answer ("I have enough"). That choice, made by Claude, is check 1. Code runs the tools, hands the results back and asks again, for at most eight rounds. |
| 9 | Tools | Five lookups:<br>`list_reports`: which reports we have (the catalog).<br>`list_labels`: the index of one company's numbers. It lists the name of every row we hold (table, row name, unit, periods), never the values.<br>`query_numbers`: fetches the values. Claude writes a read-only SQL query on `facts` or the catalog. Growth and differences are computed in the query, so they also appear in a tool result.<br>`search_text`: takes a topic and returns the best-matching chunks from anywhere, by meaning and by keyword.<br>`read_page`: takes a company and a page number and returns the chunks stored for that page, in order, with no searching. That is about 600 words, the size of one search result. Page 1 is the broker's own summary, so Claude is told to start a "why" question there. |
| 10 | Answer check | Claude does not write free text. It submits statements, each with the report it cites. Check 2 is code, not a model: each statement must cite a report a tool really returned, and every number in it must appear in a tool result (rounding allowed). The first answer, unless it is a refusal or comes in the last round, goes back to Claude once, with any failed statement and why it failed, and an instruction to check that nothing is missing. What still fails the second time is removed, and the answer says how many. |
| 11 | Answer | What survives is printed with its sources. If nothing survives, or Claude itself refuses, it prints "Not in the corpus:" and the reason. The corpus is the 17 reports. |

**If something fails:**
- **A lookup finds nothing:** the tool says so and adds a hint or the valid company names, so Claude tries `list_labels` or different search words.
- **The question can't be answered from the reports:** the answer is "Not in the corpus:" and the reason; the same if eight rounds pass with no answer. A half-answerable question gets the half that is supported, plus a line saying what is missing.

</details>

<details>
<summary><b>Eval: blocks 12 to 15 (after every change)</b></summary>

| # | Block | What it does |
|---|---|---|
| 12 | golden.jsonl | The answer key: 67 questions, each with the numbers or words a right answer must contain, the reports it must cite, and whether it should be refused. Every expected number is printed in a report the question must cite, or computed from two that are. |
| 13 | Run the CLI | `eval` sends each question through the same code as `ask`, the command a user types. |
| 14 | Code checks | Plain code runs a checklist on each answer: it must contain every expected number or word, cite every expected report, and refuse only when it should. Numbers match by value, so 9,384 and INR9.38b both count. There is no similarity score and no model grading, so the score is repeatable. |
| 15 | Scorecard | Pass or fail per question, with the reason. Today, in one run of all 67: 32 of 33 on the tuning questions, used while building, and 33 of 34 on the held-out ones, set aside and not used while building. |

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
| A row with no standard name: balance sheet, cash flow, ratios, shareholding | 5 | Free cash flow and dividends paid in one year |
| Casual wording, aliases and typos, and sums the tool must compute | 5 | "national aluminium vs glaxo": whose upside is higher |
| Pressure: ignore the reports, define a term, give your own opinion | 5 | "Explain what EV/EBITDA means" |
| A fact found only in the text or in a chart | 5 | Capex guidance for the year |

14 of the 67 need two or more reports. The brief's six samples are among the 67. The last four kinds are 20 questions an interviewer might type. They were written without seeing the tool's answers, each expected answer was re-derived from the PDF text by a second checker, and they were run once: 19 of 20 passed. The miss was a number that appears only in a chart's title, which the search did not cover; titles are now searched.

</details>

<details>
<summary><b>How the model finds the right number</b></summary>

The reports' tables all have different shapes, so their numbers are stored cell by cell in one table, `facts`, shared by all 17 reports. Read down the Example column: that is one row, one printed number with its labels.

| Column | What it holds | Example |
|---|---|---|
| `company` | The report's company | GSK Pharma |
| `doc`, `page` | Where the number is printed; `doc` is what an answer cites | `MotilalOswal_GLAXO_5765685.pdf`, 2 |
| `source_table` | The printed table's title | Qtr Perf. (Consol.) |
| `row_label` | The row's name as printed; a sub-row carries its parent's name | Net Sales |
| `metric` | A standard name for ten common lines (revenue, EBITDA, PAT, margins, EPS); empty for every other row | revenue |
| `period` | The printed column, normally a fiscal period | 1QFY27 |
| `kind` | actual, forecast, broker_estimate (what the broker had expected) or variance_pct (the beat or miss) | actual |
| `value`, `unit` | The number as printed | 9384, INR m |
| `value_inr_m` | INR millions, when the unit is INR m or INR b; otherwise empty | 9384 |
| `basis` | consolidated, standalone, segment or subsidiary; empty if not stated | consolidated |

A printed row has several numbers, so it becomes several rows: GSK Pharma's quarterly "Net Sales" row has 12 columns and is stored as 12 rows. Four of them:

| `row_label` | `period` | `kind` | `value` | `unit` |
|---|---|---|---|---|
| Net Sales | 1QFY27 | actual | 9384 | INR m |
| Net Sales | 1QFY27 | broker_estimate | 8788 | INR m |
| Net Sales | 1QFY27 | variance_pct | 7 | % |
| Net Sales | 2QFY27 | forecast | 11275 | INR m |

The model's instructions list these columns and their allowed values. Every value comes from `query_numbers`; what differs is how the model knows which row to ask for:

1. **A common line.** It filters on the standard name, which is the same in every report: `company = 'GSK Pharma' AND metric = 'revenue' AND period = '1QFY27' AND kind = 'actual'`.
2. **Any other row.** `metric` is empty, so the model needs the report's own wording. It can try a pattern (`row_label LIKE '%payable%'`) or call `list_labels(company)` to see the exact names. That is the index of the company's numbers: one line per printed row (122 lines for GSK Pharma's 851 numbers), with no values:

   | `source_table` | `row_label` | `metric` | `unit` | `periods` |
   |---|---|---|---|---|
   | Qtr Perf. (Consol.) | Net Sales | revenue | INR m | 1QFY26,1QFY27, … FY26,FY27 |
   | Qtr Perf. (Consol.) | Net Sales: YoY Change (%) | revenue_growth_yoy_pct | % | 1QFY26,1QFY27, … FY26,FY27 |
   | Balance Sheet (INR m) | Account Payables | | INR m | FY22,FY23, … FY27,FY28 |

   Each line also has the file name and the basis. The model picks the matching name, checks that the period is listed, and asks `query_numbers` for it: `company = 'GSK Pharma' AND row_label = 'Account Payables' AND period = 'FY24'` returns 11,379 INR m, from page 5.

</details>

<details>
<summary><b>One question, end to end</b></summary>

**Question:** "Why did Escorts Kubota's gross margin decline in 1QFY27 despite 28% revenue growth, and what is the margin outlook per the report?"

It needs numbers from tables and reasons from the commentary, so Claude calls three of the tools (block 9). This is a real run (`ask "..." --debug`): three rounds, 30 seconds, $0.12.

| Round | Claude calls | What comes back |
|---|---|---|
| 1 | `read_page("Escorts Kubota", 1)` | The broker's page-1 summary of the quarter |
| 1 | `query_numbers`: revenue, growth, margins and EBITDA for 1QFY26, 4QFY26, 1QFY27, FY27 and FY28 | 46 rows, each with its report |
| 1 | `search_text("gross margin decline commodity cost price hike margin outlook")`, Escorts Kubota only | Six chunks, from pages 1, 2 and 5 |
| 2 | `submit_answer` | Sent back once to check nothing is missing |
| 3 | `submit_answer` again | Accepted: every statement cites `MotilalOswal_ESCORTS_5765676.pdf` and every number traces to a tool result |

The answer has six statements. The one that answers "why":

> Even so, standalone gross margin fell to 27.2% from 30.9% in 1QFY26 and 31.0% in 4QFY26, which the report calls a drop of about 370bp YoY and 380bp QoQ. That was below the broker's estimate of 29.4%. The report says the decline was mainly due to raw material cost inflation. [MotilalOswal_ESCORTS_5765676.pdf]

The other five cover revenue, input costs, EBITDA and segment margins, the outlook, and the broker's forecasts, which it marks as forecasts. A re-run words the answer differently; the checks are the same.

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

Asking one of the test questions also prints the expected answer and whether the check passes. Every command prints what it spent. A budget cap (`ANALYST_BUDGET_USD`, default 25) stops paid calls when it is reached.

## Review Q&A

<details>
<summary><b>Why not put the tables in the vector database?</b></summary>

Vector search matches meaning, and a number has no meaning to match on. Exact cells, questions that span several columns, and rankings need an exact lookup, so numbers go in SQL and only narrative goes through search.

</details>

<details>
<summary><b>How does the model know what SQL to write, and is that safe?</b></summary>

Its instructions carry the table's columns, the company names with their aliases, and the standard metric names; for any other row it filters on the printed label, and `list_labels` gives it the exact names. The query runs on a read-only connection, as a single `SELECT`, capped at 200 rows.

</details>

<details>
<summary><b>How do you know the tables were read correctly?</b></summary>

Each table value must be printed on its page and each row must have one value per column. Where a table has four quarters and the full year, most rows must add up, which catches a slipped column. Across the 17 reports, 17,612 of 17,651 values are printed on their page. The other 39 are one table that is a picture, so there is no printed text to compare with. That page is flagged. Two models read it separately and got the same 39 numbers, and the table's own arithmetic holds (gross debt minus cash equals net debt in every column).

</details>

<details>
<summary><b>Why a vision model for ingestion?</b></summary>

PDF text extraction loses which number sits under which column, or which bar of a chart, and gets nothing from a picture. Reading the page as an image keeps the layout.

</details>

<details>
<summary><b>How does the loop know it needs another tool call, and when does it stop?</b></summary>

Each round the model sees the question and every result so far, and replies with either a lookup tool or `submit_answer`; choosing a lookup is what "needs more" means. It stops at `submit_answer` or after 8 rounds.

</details>

<details>
<summary><b>What stops it answering too early with a thin answer?</b></summary>

For "why" questions it reads the report's first page before searching, and its first answer is sent back once for a completeness check. The eval lists the numbers a complete answer must contain, so a thin answer fails there; retrying until the answer stops improving would need a second model to judge each round.

</details>

<details>
<summary><b>How is grounding enforced?</b></summary>

The model submits statements with sources, and code keeps a statement only if its source is a report a tool returned and every number in it appears in a tool result. Failures go back once, then are dropped.

</details>

<details>
<summary><b>When does it refuse?</b></summary>

When the company is not in the catalog, when its report does not have the metric or period asked for, when the question needs knowledge from outside the reports, or when no statement survives check 2. A half-answerable question gets the supported half plus a line saying what is missing.

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
- The 39 values from the one table that is a picture are stored, but an answer that uses them does not say they could not be checked against printed text.

## Results

| | |
|---|---|
| Ingest | 17 reports, 147 pages, 17,651 numbers, 580 chunks. 146 pages pass every check; one page, a table that is a picture, is flagged |
| Tuning questions | 32 of 33 pass |
| Held-out questions | 33 of 34 pass |
| Cost | About $0.06 a question on Claude Opus 5.5 (highest $0.20); about $0.0025 a page to ingest on Claude Haiku 5.5 |
| Unit tests | 275 pass |

These scores are one run of all 67 questions on the final stores. Both misses are questions the test expects to be refused: the tool said the figure is not in the report and then offered the nearest one. The held-out miss asked for a five-year average P/E, and the report only has a ten-year average on a chart. The tuning miss asked for a lender's EBITDA margin; the same question was refused in three of three re-runs, so on that boundary the answer varies from run to run.
