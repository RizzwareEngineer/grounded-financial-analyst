# System design

The diagram is in the [README](README.md#how-it-works) and as an [Excalidraw file](design/system-design.excalidraw) ([open it in Excalidraw](https://excalidraw.com/#json=NVlGfcGPt06jvL7l9KLEB,ayQ9MlwI-SWJxA14YqS2Zg)).

## What it has to do

- Answer factual, analytical and qualitative questions about one company, several companies, or a sector.
- Cite the source document for every claim. An uncited claim counts as wrong.
- Say so when the reports don't contain the answer. No guessing, no general knowledge.

## Components

| Diagram box | What it is | File |
|---|---|---|
| Agent loop | Sends the question to Claude, runs the tool it asks for, feeds the result back, up to 8 rounds | `analyst/agent.py` |
| Four tools | `list_reports`, `list_labels`, `query_numbers`, `search_text` | `analyst/tools.py` |
| Answer check | Code that traces every number and source in the draft answer | `analyst/agent.py` |
| Catalog, facts, keyword index | SQLite tables `reports`, `facts`, `chunks` + FTS5 | `analyst/store.py` |
| Vectors | Chroma with the all-MiniLM-L6-v2 embedding model | `analyst/store.py` |
| Read each page | PyMuPDF text plus a Claude vision call per page | `analyst/pdf.py`, `analyst/ingest.py` |
| Check numbers, split, embed, load | Code checks, chunking, loading | `analyst/ingest.py` |
| CLI | `ingest`, `ask`, `eval` | `analyst/__main__.py` |
| Eval | Golden questions and a code scorer | `evals/golden.jsonl`, `analyst/evalcheck.py` |
| Cost meter | Records every Claude call and enforces the budget | `analyst/llm.py` |

## Data model

**`reports`**: one row per PDF. Company, aliases, sector label, what the business does, report type and date, reported period, rating, current price, target price, stated upside, valuation basis.

**`facts`**: one row per table cell.

| Column | Example | Why it exists |
|---|---|---|
| `company`, `doc`, `page` | GSK Pharma, `MotilalOswal_GLAXO_5765685.pdf`, 2 | The citation |
| `source_table`, `row_label` | Quarterly performance, Net Sales | The report's own wording |
| `metric` | `revenue` | A standard name for the dozen lines every report has; empty otherwise |
| `period`, `kind` | `1QFY27`, `actual` | `kind` is actual, forecast, broker_estimate or variance_pct |
| `value`, `unit` | 9384, INR m | As printed |
| `value_inr_m` | 9384 | Money totals converted to one unit in code |
| `basis` | consolidated | So comparisons can say when bases differ |

**`chunks`**: narrative sections, whole tables rendered as text, and chart descriptions. Each has `content` (what the model reads) and `search_text` (what is embedded and keyword-indexed).

## Tools

| Tool | Reads | Returns |
|---|---|---|
| `list_reports()` | `reports` | The catalog |
| `list_labels(company)` | `facts` | Every table, row label, unit and period that report has |
| `query_numbers(sql)` | `facts`, `reports` | Rows from one read-only `SELECT`; on zero rows, the valid company names and a hint |
| `search_text(query, companies)` | `chunks` | The best chunks by vector similarity and BM25, merged; one search per company |

Every row and chunk carries its document and page, so citations come from data.

## Agent loop and answer check

1. The system prompt is built from the database: the catalog, the table columns, the allowed values, example queries and the grounding rules.
2. Each round the model either calls tools or calls `submit_answer` with a list of claims, each with its source documents.
3. Code checks the submission:
   - a claim's sources must be documents a tool returned in this run;
   - every number in a claim must trace to a number in a tool result, allowing rounding and million/billion or ratio/percent scaling;
   - failures go back to the model once; what still fails is dropped; if nothing survives, the answer is a refusal.
4. The printed answer is the surviving claims with their citations, plus a line for anything the corpus does not contain.

## Ingest and its checks

Each content page (disclosure pages are skipped) is sent to Claude as a one-page PDF. The reply is structured: narrative blocks, tables as columns plus rows of values, chart descriptions, and on page 1 the report card.

| Check (code) | Catches |
|---|---|
| Every row has exactly one value per column | A blank cell that shifted the rest of the row |
| Every value is printed on that page | A misread or invented number |
| Four quarters sum to the full year | A value placed under the wrong period |
| Forecast set from the reported quarter | Forecast columns printed with no "E" |

A failing page is retried, then escalated to a stronger model, then reported as failed. Page results are cached on disk, so a re-run costs nothing.

## The two concerns from the design review

| Concern | Fix | Tradeoff |
|---|---|---|
| Vector search over numeric tables is inaccurate: multi-column questions, split tables, too many numbers handed to the model | Numbers in SQL, one row per cell; the model asks for exactly the rows it needs | More work at ingest, and a misread cell becomes a wrong answer, hence the checks above |
| One retrieval attempt cannot fit every question: a lookup needs one report, a sector question needs several | A bounded loop where the model sees each result and can try another way | Slower, costlier and less predictable than a fixed pipeline; capped at 8 rounds and checked by code |

## Left out on purpose

- **Fine-tuning:** baked-in knowledge cannot be cited or refused.
- **LangChain:** it would wrap the same calls and sit between the code and the raw tool results the citation check needs.
- **Reranker:** search is already filtered to one company.
- **LLM judge in the eval:** numbers, sources and refusals are checked by code; "why" answers are read by hand.
