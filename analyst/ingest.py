"""Ingest: Claude reads each content page of each report into structured JSON; code checks it and loads the stores."""
import base64
import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from itertools import groupby
from pathlib import Path

import anthropic
import pymupdf

from analyst import llm, pdf, store

DEFAULT_MODEL = "claude-haiku-5-5"
ESCALATION_MODEL = "claude-sonnet-5-5"  # reads a page again when the default model's reading fails the checks
EFFORT = "low"
PASS_RATE = 0.97  # share of a page's table values that must be printed on that page
MAX_WORDS = 160  # R5: the embedding model reads only about 256 tokens
CHART_PREFIX = "Chart (values read from an image, approximate): "

PROMPT = f"""Transcribe the attached page of an Indian broker research report into structured data. \
Transcribe only: never summarise, round, convert or compute anything.

CARD. Fill `card` only if this is the report's first page (the one with the rating, CMP and target price); otherwise null.
- company: the short name the report itself uses as its title, e.g. "APL Apollo Tubes".
- aliases: every other name, abbreviation and the Bloomberg ticker the report uses for the company, comma-separated, e.g. "APAT, APAT IN".
- sector: verbatim after "Sector:".
- business: one line on what the company does, in the report's words.
- report_type: e.g. "1QFY27 Results Update". report_date: as printed.
- reported_period: the quarter whose results the report covers, e.g. "1QFY27". For a report with no quarter results, the latest full year with actuals, e.g. "FY26".
- rating, cmp, target_price: as printed, numbers without separators. upside_stated: as printed, e.g. "+15%".
- valuation_basis: how the target price is derived, in the report's words, e.g. "35x FY28E EPS".

BLOCKS. Everything else on the page, in reading order, as text, table and chart blocks. Skip page headers and footers, \
analyst contact lines and disclosure boilerplate, including tables of pure disclosure boilerplate.

TEXT block. section: the nearest heading as printed. text: verbatim, one paragraph or bullet per line, bullets kept. \
A box of labelled facts whose values are not single numbers (Bloomberg code, 52-week range) is a text block, one "label: value" per line.

TABLE block. Every table of numbers, including the small ones in the margin. Two tables printed side by side are two blocks.
- title: as printed, with its exhibit number if it has one.
- unit: the unit printed for the table. A money unit must be written exactly "INR m", "INR b", "USD m" or "USD b" \
("(INRm)" and "INR Million" are "INR m"). Any other unit as printed. null if none is printed.
- basis: consolidated or standalone as the table, page or report says; segment for a table of business segments; \
subsidiary for a table about one subsidiary; null if nothing says.
- description: one line for search: the company, what the table is, the unit and which periods it covers.
- columns: every data column once, left to right.
  period: fiscal periods are written like 1QFY27 or FY26 with no trailing E. A "1Q" under an "FY26" group header is 1QFY26; \
"FY27E" is FY27; "Q1FY27E" or "1QE" is 1QFY27. Any other header as printed.
  kind: actual for reported figures. forecast for a future period or a header marked E. broker_estimate for the column \
showing the broker's own earlier estimate for the reported quarter (it sits next to the variance column and repeats the \
reported quarter's period). variance_pct for the beat/miss column ("Var %"), with the reported quarter as its period.
- rows: every row that has at least one value, top to bottom.
  label: as printed. A sub-row such as "YoY Change (%)" or "Margins (%)" carries its parent line in the label, \
e.g. "Gross Sales: YoY Change (%)", because the same sub-label repeats under several lines.
  metric: one of {", ".join(store.METRICS)}; set it only when the row is unambiguously that line for the whole company, \
otherwise null. Sales, net sales and revenue from operations are revenue; "Gross Sales: YoY Change (%)" is revenue_growth_yoy_pct; \
"EBITDA: Margins (%)" is ebitda_margin_pct. Segment lines, per-unit figures and anything doubtful get null: \
a lender's "Net Income" that is really net total income gets null.
  unit: null, unless the row's unit differs from the table's, then that unit, e.g. "%", "x", "INR", "days".
  values: exactly one entry per column, in column order. A blank cell is null and nothing is shifted left or right to fill it: \
when a row prints fewer values than there are columns, use the alignment to decide which columns they sit under. \
Numbers keep their sign (parentheses mean negative) and have no thousands separators or % sign; Indian grouping 2,30,790 means 230790.

CHART block. title: as printed. description: what is plotted, including any values that are clearly readable, with their periods. \
search: one line for search: the company, what the chart shows and which periods."""


def _or_null(schema):
    return {"anyOf": [schema, {"type": "null"}]}


def _object(**properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


_STR, _NUM = {"type": "string"}, {"type": "number"}
SCHEMA = _object(
    card=_or_null(_object(
        company=_STR, aliases=_STR, sector=_STR, business=_STR, report_type=_STR, report_date=_STR, reported_period=_STR,
        rating=_or_null(_STR), cmp=_or_null(_NUM), target_price=_or_null(_NUM), upside_stated=_or_null(_STR),
        valuation_basis=_or_null(_STR))),
    blocks={"type": "array", "items": {"anyOf": [
        _object(type={"type": "string", "enum": ["text"]}, section=_STR, text=_STR),
        _object(type={"type": "string", "enum": ["table"]}, title=_STR, unit=_or_null(_STR),
                basis=_or_null({"type": "string", "enum": store.BASES}), description=_STR,
                columns={"type": "array", "items": _object(period=_STR, kind={"type": "string", "enum": store.KINDS})},
                rows={"type": "array", "items": _object(
                    label=_STR, metric=_or_null({"type": "string", "enum": store.METRICS}), unit=_or_null(_STR),
                    values={"type": "array", "items": _or_null(_NUM)})}),
        _object(type={"type": "string", "enum": ["chart"]}, title=_STR, description=_STR, search=_STR),
    ]}},
)
# A change to the prompt, schema or effort makes every cached page stale.
KEY = hashlib.sha256((PROMPT + json.dumps(SCHEMA, sort_keys=True) + EFFORT).encode()).hexdigest()[:10]


def page_pdf(path, page_number):
    """One page of a PDF as its own one-page PDF, in memory."""
    with pymupdf.open(path) as src, pymupdf.open() as one:
        one.insert_pdf(src, from_page=page_number - 1, to_page=page_number - 1)
        return one.tobytes()


def read(client, page_bytes, model):
    """One Claude call for one page. data is None when the call failed, was refused or was cut off."""
    llm.guard()
    start = time.time()
    out = {"model": model, "data": None, "error": None, "usd": 0.0}
    try:
        resp = client.messages.create(
            model=model, max_tokens=16000,
            output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": [
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                "data": base64.standard_b64encode(page_bytes).decode()}},
                {"type": "text", "text": PROMPT}]}])
    except anthropic.APIError as e:
        out["error"] = f"{type(e).__name__}: {e}"
    else:
        out["usd"] = llm.record(model, resp.usage, "ingest")
        if resp.stop_reason == "end_turn":
            out["data"] = json.loads(next(b.text for b in resp.content if b.type == "text"))
        else:
            out["error"] = resp.stop_reason  # "max_tokens" (cut off) or "refusal": the content is not usable JSON
    out["seconds"] = round(time.time() - start, 1)
    return out


def quarter_sum(table):
    """R1, a column shift: rows in a money unit whose four quarters do not add up to the full year within 1%."""
    col = {}
    for i, c in enumerate(table["columns"]):
        if c["kind"] in ("actual", "forecast"):
            col.setdefault(store.clean_period(c["period"]), i)
    bad = []
    for year in [p for p in col if re.fullmatch(r"FY\d\d", p) and all(f"{q}Q{p}" in col for q in "1234")]:
        for row in table["rows"]:
            if (row["unit"] or table["unit"]) not in store.MONEY_UNITS or len(row["values"]) != len(table["columns"]):
                continue
            quarters, full = [row["values"][col[f"{q}Q{year}"]] for q in "1234"], row["values"][col[year]]
            if None not in quarters and full is not None and abs(sum(quarters) - full) > 0.01 * abs(full):
                bad.append({"table": table["title"], "row": row["label"], "year": year, "quarters": quarters, "full_year": full})
    return bad


def check_page(page, page_text):
    """The code checks on one page's JSON. `ok` (structure and printed) drives the retry; quarter-sum violations are recorded."""
    structure, not_printed, shifted, values = [], [], [], 0
    for table in (b for b in page["blocks"] if b["type"] == "table"):
        for row in table["rows"]:
            if len(row["values"]) != len(table["columns"]):
                structure.append(f'{table["title"]} / {row["label"]}: {len(row["values"])} values for {len(table["columns"])} columns')
            for v in row["values"]:
                if v is not None:
                    values += 1
                    if not pdf.on_page(v, page_text):
                        not_printed.append([table["title"], row["label"], v])
        shifted += quarter_sum(table)
    rate = 1 - len(not_printed) / values if values else 1.0
    return {"ok": not structure and rate >= PASS_RATE, "structure": structure, "values": values,
            "not_printed": not_printed, "pass_rate": rate, "quarter_sum": shifted}


def _ok(result):
    return bool(result["checks"] and result["checks"]["ok"])


def _score(result):
    c = result["checks"]
    return (1, -len(c["structure"]), c["pass_rate"]) if c else (0,)


def read_checked(client, doc, page_number, page_text, page_bytes, model, cache, tries=2):
    """One model's checked reading of one page: from the cache, else up to `tries` calls. Returns (result, dollars spent now)."""
    file = cache / f"{doc}.p{page_number}.{model}.{KEY}.json"
    if file.exists():
        return json.loads(file.read_text()), 0.0
    best, spent = None, 0.0
    for _ in range(tries):
        result = read(client, page_bytes, model)
        spent += result["usd"]
        result["checks"] = check_page(result["data"], page_text) if result["data"] else None
        if best is None or _score(result) > _score(best):
            best = result
        if _ok(best):
            break
    if best["data"]:  # a refused or cut-off page is not cached, so the next run tries it again
        file.write_text(json.dumps(best))
    return best, spent


def read_page(client, doc, page_number, page_text, page_bytes, model, cache):
    """The best reading of a page: the given model, retried once, then the stronger model if the checks still fail."""
    result, spent = read_checked(client, doc, page_number, page_text, page_bytes, model, cache)
    if not _ok(result) and model != ESCALATION_MODEL:
        stronger, more = read_checked(client, doc, page_number, page_text, page_bytes, ESCALATION_MODEL, cache, tries=1)
        spent += more
        if _score(stronger) > _score(result):
            result = stronger
    return {**result, "spent": spent}


def facts_from(page_number, table):
    """One fact per non-null cell, in the shape store.add_facts takes."""
    return [{"page": page_number, "source_table": table["title"], "row_label": row["label"], "metric": row["metric"],
             "period": c["period"], "kind": c["kind"], "value": v,
             # the beat/miss column is a percentage whatever the row's unit
             "unit": "%" if c["kind"] == "variance_pct" else row["unit"] or table["unit"], "basis": table["basis"]}
            for row in table["rows"] for c, v in zip(table["columns"], row["values"]) if v is not None]


def table_markdown(table, reported_period):
    def header(c):
        kind = store.fact_kind(c["period"], reported_period, c["kind"])  # forecast by date, as the facts are stored
        return {"forecast": f'{c["period"]} (E)', "broker_estimate": f'{c["period"]} est.', "variance_pct": "var %"}.get(kind, c["period"])

    lines = [f'**{table["title"]}** (unit: {table["unit"] or "not stated"}; basis: {table["basis"] or "not stated"})',
             "| | " + " | ".join(header(c) for c in table["columns"]) + " |",
             "|---" * (len(table["columns"]) + 1) + "|"]
    lines += [f'| {row["label"]} | ' + " | ".join("" if v is None else str(v) for v in row["values"]) + " |" for row in table["rows"]]
    return "\n".join(lines)


def split_text(text, cap=MAX_WORDS):
    """Pieces of at most `cap` words, broken at line ends where a line fits."""
    lines = []
    for line in text.splitlines():
        words = line.split()
        lines += [" ".join(words[i:i + cap]) for i in range(0, len(words), cap)]
    pieces, current, count = [], [], 0
    for line in lines:
        n = len(line.split())
        if current and count + n > cap:
            pieces.append("\n".join(current))
            current, count = [], 0
        current.append(line)
        count += n
    return pieces + ["\n".join(current)] if current else pieces


def chunks_from(doc, page_number, card, blocks):
    """Search chunks for one page, in the shape store.add_chunks takes."""
    chunks = []

    def add(section, kind, content, search_text):
        chunks.append({"id": f"{doc}:{page_number}:{len(chunks)}", "company": card["company"], "doc": doc, "page": page_number,
                       "section": section, "kind": kind, "content": content, "search_text": search_text})

    for (kind, section), group in groupby(blocks, key=lambda b: (b["type"], b.get("section"))):
        if kind == "text":
            for piece in split_text("\n".join(b["text"] for b in group)):
                add(section, "text", piece, f'{card["company"]} | {card["report_type"]} | {section}\n{piece}')
            continue
        for b in group:
            if kind == "table":
                add(b["title"], "table", table_markdown(b, card["reported_period"]),
                    b["description"] + "\n" + ", ".join(row["label"] for row in b["rows"]))
            else:
                add(b["title"], "chart", CHART_PREFIX + b["description"], b["search"])
    return chunks


def run(docs_dir="docs", only=None, workers=4, model=None, data=store.DATA):
    """Ingest every report in docs_dir whose file name contains `only`. Cached pages make no API call."""
    model = model or DEFAULT_MODEL
    client = llm.client()
    cache = Path(data) / "cache" / "pages"
    cache.mkdir(parents=True, exist_ok=True)
    conn, coll = store.connect(Path(data)), store.collection(Path(data))
    out = {"docs": 0, "pages": 0, "facts": 0, "chunks": 0, "failed_pages": [], "cost_usd": 0.0, "checks": []}
    with ThreadPoolExecutor(workers) as pool:
        for path in sorted(Path(docs_dir).glob("*.pdf")):
            doc = path.name
            if only and only not in doc:
                continue
            # pymupdf is not thread-safe, so the pages are cut here and only the API calls run in the pool
            pages = [(n, text, page_pdf(path, n)) for n, text in pdf.content_pages(path)]
            results = list(pool.map(lambda p: read_page(client, doc, *p, model, cache), pages))
            out["pages"] += len(pages)
            out["cost_usd"] += sum(r["spent"] for r in results)
            for (n, _, _), r in zip(pages, results):
                out["checks"].append({"doc": doc, "page": n, "model": r["model"], "error": r["error"], **(r["checks"] or {})})
                if not _ok(r):
                    out["failed_pages"].append({"doc": doc, "page": n, "reason": r["error"] or "failed the checks; its values are stored"})
            card = results[0]["data"] and results[0]["data"]["card"]
            if not card:
                out["failed_pages"].append({"doc": doc, "page": pages[0][0], "reason": "no report card on the first page; report not loaded"})
                print(f"{doc}: FAILED, no report card on the first page; ${out['cost_usd']:.2f} so far")
                continue
            card = {**card, "doc": doc, "reported_period": store.clean_period(card["reported_period"])}
            facts, chunks = [], []
            for (n, _, _), r in zip(pages, results):
                blocks = r["data"]["blocks"] if r["data"] else []
                facts += [f for b in blocks if b["type"] == "table" for f in facts_from(n, b)]
                chunks += chunks_from(doc, n, card, blocks)
            store.clear_doc(conn, coll, doc)
            store.add_report(conn, card)
            store.add_facts(conn, card["company"], doc, card["reported_period"], facts)
            store.add_chunks(conn, coll, chunks)
            conn.commit()
            out["docs"] += 1
            out["facts"] += len(facts)
            out["chunks"] += len(chunks)
            checked = [r["checks"] for r in results if r["checks"]]
            values = sum(c["values"] for c in checked)
            rate = 1 - sum(len(c["not_printed"]) for c in checked) / values if values else 1.0
            print(f"{doc}: {len(pages)} pages, {len(facts)} facts, {len(chunks)} chunks, "
                  f"values printed on their page {rate:.1%}, ${out['cost_usd']:.2f} so far")
    conn.close()
    return out
