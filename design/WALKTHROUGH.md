# Verbal walkthrough

A script for presenting the design, then one question followed through every box of the [diagram](../README.md#how-it-works).

## The one-minute version

> "It's a command-line analyst over 17 broker reports. There are three parts: ingest, query, and eval.
>
> Ingest runs once. Each page is read by a vision model and checked by code. Every table cell becomes a row in a SQL table; narrative goes into a vector and keyword index.
>
> At query time the model works in a loop with four tools. It looks numbers up with SQL, finds explanations with search, and can try again when something is missing. Before the answer is shown, code confirms every number and every source came from a tool result. If nothing supports an answer, it says the corpus doesn't contain it.
>
> The eval is 44 questions scored by code on numbers, sources and refusals."

## The full walkthrough

### Ingest

> "A PDF only stores characters at positions, so plain text extraction loses which number is in which column, and charts come out as nothing. So each page goes to a vision model, which returns the page as structured data: text blocks, tables as columns and rows, and a description of each chart.
>
> I don't trust that output until code has checked it. Every number has to be literally printed on that page. Every row has to have one value per column. Where a table has four quarters and a full year, the quarters have to add up. A page that fails is retried, then escalated to a stronger model.
>
> Then the data is split two ways. Table cells go into SQLite as one row per number, with the company, the row label, the period, the unit, whether it's an actual or a forecast, and the document and page. Narrative is cut into chunks by section, embedded with MiniLM into Chroma, and indexed for keyword search. Tables and charts get a one-line description so search can find them."

### Query

> "A question goes to the agent loop. The model has four tools: list the reports, list the row labels a report has, run a SQL query over the numbers, and search the text.
>
> After every tool result the model sees the question and everything returned so far, and decides whether it has enough. If a query comes back empty, or it asked about three companies and got two, it tries another way. That is capped at eight rounds.
>
> The model doesn't write the final text freely. It submits claims, each with its source documents. Code then checks that each source is a document a tool actually returned, and that every number traces to a tool result. Anything that fails goes back once, then is dropped. If nothing survives, the answer is 'not in the corpus'."

### Eval

> "I have 44 questions: the six samples plus lookups, derivations, rankings with unit traps, sector questions, and questions that should be refused. Each has the numbers the answer must contain and the documents it must cite. Code scores them. I verified the expected values against the PDF text, and held 15 questions back from tuning."

## One question, end to end

**Question:** "Why did Escorts Kubota's gross margin decline in 1QFY27 despite 28% revenue growth, and what is the margin outlook?"

It needs a number from a table and a reason from the commentary, so it touches both stores.

### Before the question: how the data got there

| Source in the PDF | What ingest stored |
|---|---|
| The key performance table's gross margin row | A `facts` row: Escorts Kubota, gross margin, 1QFY27, 27.2, %, actual, with document and page |
| The quarterly table's sales row and its growth | `facts` rows: revenue 31,789 INR m and revenue growth 28.0% |
| The paragraph on raw-material costs | A text chunk in Chroma and the keyword index, tagged Escorts Kubota |

### The loop

| Round | The model does | What comes back | What it concludes |
|---|---|---|---|
| 1 | `query_numbers`: gross margin and revenue growth for Escorts Kubota, 1QFY27 | 27.2% and 28.0%, each with document and page | It has the "what", not the "why" |
| 2 | `search_text("gross margin decline reason", Escorts Kubota)` | Chunks on margins and raw-material cost inflation | It has the reason; the outlook is still missing |
| 3 | `search_text("margin outlook second half price hike", Escorts Kubota)` | The outlook commentary | Every part of the question now has support |
| 4 | `submit_answer` with claims and sources | | |

If round 2 had returned only chunks about volumes, the model would have searched again with different words. That retry is the loop doing its job.

### The check

- Each claim cites `MotilalOswal_ESCORTS_5765676.pdf`, which the tools returned in rounds 1 to 3, so the sources pass.
- 27.2, 28 and 370bp all appear in tool results, so the numbers pass.
- A sentence such as "margins typically recover within two quarters" has no tool result behind it and would be dropped.

### The answer

> Gross margin fell about 370bp YoY to 27.2%, mainly because of raw-material cost inflation, even though revenue grew 28%. [MotilalOswal_ESCORTS_5765676.pdf]
> The report expects pressure to continue in the second quarter and ease in the second half as input costs cool. [MotilalOswal_ESCORTS_5765676.pdf]

This trace shows the intended path. The real `--debug` output for this question replaces it once the build passes its checks.
