"""The agent loop and the answer check: the model works through tools, then code verifies what it submits."""
import json
import re
import sys

from . import llm, store, tools

MODEL = "claude-opus-5-5"
MAX_ROUNDS = 8
NO_ANSWER = "the reports do not contain an answer to this question."

RULES = """You are a financial analyst answering questions about a fixed set of broker research reports (the corpus).
You work only through the tools, and you finish by calling submit_answer. Plain text you write is discarded.

# Grounding rules
- Use only what the tools return. No outside or general knowledge, including definitions. If the tools do not support an answer, refuse.
- Numbers come from query_numbers. Use metric names for the common lines and list_labels(company) to find any other row. Compare or rank companies on value_inr_m or on printed percentages, never on raw values in different units. Every derived number (growth, beat, upside, difference, ratio) must be computed in a SQL query so that it appears in a tool result. Never do arithmetic yourself.
- kind 'actual' is reported; 'forecast' is the broker's projection for a period not yet reported; 'broker_estimate' is what the broker expected for the reported quarter. Never present a forecast or an estimate as an actual. "Last quarter" means each report's reported_period.
- State the basis (consolidated or standalone) when comparing companies whose bases differ.
- Reasons, outlook and commentary come from search_text, filtered to the company; try different wording before concluding something is absent.
- Sector questions: membership is the sector column of the catalog and nothing else. Open with the scope ("the corpus has three reports labelled X: ..."), report each company separately, and mention in one line any company whose business sounds related but carries a different label.
- If a tool returns nothing, try another way (list_labels, different wording, another table) before giving up. If a report gives two different values for the same thing, report both and say so.
- Partly answerable questions: answer the supported part and say in `missing` what the corpus does not contain. A wrong premise in the question is corrected from the report, with a citation.
- Refuse (refused true, no claims) when the company is not in the catalog, the metric or period is not in its report (a period after reported_period has not been reported, whatever the broker forecasts for it), or the question needs knowledge from outside the reports.

# Working method
- You have at most {rounds} rounds. Call several tools in one round whenever the calls do not depend on each other.
- Before submitting, check every part of the question against your results. A "why" or "how did it do" question needs both the reasons the report gives and the reported quarter's headline figures behind them (revenue, margin, profit, order wins, each against the broker's estimate where printed): query the numbers and search the results summary and the valuation section, not just one of them.
- Always select doc in a query: a claim can only cite documents that appeared in your tool results.
- `missing` names only something the question explicitly asks for that the corpus does not contain; otherwise leave it empty. Do not list detail the question did not ask about, and put caveats in a claim, not there.
- Every number you write is checked in code against your tool results (rounding, INR m against INR b, and a ratio shown as a percentage are allowed). A number that is not in a tool result gets its claim removed.

# Catalog (the reports table; `company` is the exact name to use in queries)
{catalog}

# Tables
reports: one row per report.
- doc: the PDF file name; this is what a claim cites
- company, aliases (comma-separated other names), sector (the broker's sector label), business (one line on what the company does)
- report_type, report_date
- reported_period: the latest period with reported results in that report, such as 1QFY27
- rating, cmp (current market price, INR), target_price (INR), upside_stated (as printed), valuation_basis (how the target price is derived)
facts: one row per number printed in a table.
- company, doc, page
- source_table: the title of the table the row came from
- row_label: the label as printed; a sub-row is stored as "Parent: sub-row"
- metric: a standard name for the common lines, one of {metrics}; NULL for every other row
- period: such as 1QFY27 or FY27 (no E suffix)
- kind: one of {kinds} (variance_pct is the printed beat or miss against the broker's estimate)
- value and unit: as printed. Money totals use one of the units {money}; other units are free text such as %, x, bp
- value_inr_m: the value in INR millions for money totals in INR m or INR b, NULL otherwise
- basis: one of {bases}

# Example queries
-- Actual against the broker's estimate, with the beat computed in SQL
SELECT a.doc, a.value AS actual, e.value AS estimate, a.unit, ROUND((a.value - e.value) * 100.0 / e.value, 1) AS beat_pct
FROM facts a JOIN facts e ON e.doc = a.doc AND e.row_label = a.row_label AND e.period = a.period AND e.kind = 'broker_estimate'
WHERE a.company = 'Company A' AND a.metric = 'revenue' AND a.period = '1QFY27' AND a.kind = 'actual';
-- Rank companies on a printed percentage
SELECT company, doc, value AS growth_pct, basis FROM facts
WHERE metric = 'revenue_growth_yoy_pct' AND period = '1QFY27' AND kind = 'actual' AND company IN ('Company A', 'Company B') ORDER BY value DESC;
-- Compare sizes across reports that print different units
SELECT company, doc, value, unit, value_inr_m, basis FROM facts
WHERE metric = 'revenue' AND period = '1QFY27' AND kind = 'actual' ORDER BY value_inr_m DESC;
-- A row with no standard metric name, found by its label
SELECT doc, page, row_label, period, kind, value, unit FROM facts WHERE company = 'Company A' AND row_label LIKE '%operating income%';
-- Rating, target price and the implied upside
SELECT doc, company, rating, cmp, target_price, ROUND((target_price - cmp) * 100.0 / cmp, 1) AS upside_pct, upside_stated
FROM reports WHERE company = 'Company A';
"""

TOOLS = [
    {"name": "list_reports", "description": "Every report in the corpus with its full card (all columns of the reports table).",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "list_labels", "description": "The table rows one company's report has: source_table, row_label, metric, unit, basis "
     "and the periods available. Use it to find a row that has no standard metric name.",
     "input_schema": {"type": "object", "properties": {"company": {"type": "string", "description": "Exact company name from the catalog."}},
                      "required": ["company"], "additionalProperties": False}},
    {"name": "query_numbers", "description": "Run one read-only SQLite SELECT over the facts and reports tables. Returns at most 200 rows.",
     "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"], "additionalProperties": False}},
    {"name": "search_text", "description": "Search the report text (commentary, reasons, outlook, valuation). Returns chunks with their doc and page.",
     "input_schema": {"type": "object", "properties": {
         "query": {"type": "string"},
         "companies": {"type": "array", "items": {"type": "string"}, "description": "Exact company names to search within; one search runs per company."},
         "k": {"type": "integer", "description": "Chunks per company (default 6)."}},
         "required": ["query"], "additionalProperties": False}},
    {"name": "submit_answer", "strict": True, "description": "Finish the run. This is the only way to answer. Each claim is one or two "
     "sentences making one point; its sources are the doc values of the tool rows or chunks that support it. Write periods as the "
     "reports do (1QFY27), counts of reports as words, and no list numbering. To refuse, set refused true with no claims.",
     "input_schema": {"type": "object", "properties": {
         "refused": {"type": "boolean"},
         "claims": {"type": "array", "items": {"type": "object", "properties": {
             "text": {"type": "string"}, "sources": {"type": "array", "items": {"type": "string"}}},
             "required": ["text", "sources"], "additionalProperties": False}},
         "missing": {"type": "string", "description": "What the question asks for that the corpus does not contain; empty if nothing."}},
         "required": ["refused", "claims", "missing"], "additionalProperties": False}},
]

# Capitalised only, so "may" the verb is not read as a month.
_MONTH = (r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sep(?:t(?:ember)?)?"
          r"|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)")
_PART = r"(?:[1-4]Q|Q[1-4]|[12]H|H[12]|9M)"
# Digits that are not quantities: they are blanked before numbers are read.
_NOT_NUMBERS = re.compile("|".join([
    r"\S+\.pdf\b",                                                                # file names
    r"\b\d{4}-\d{2}-\d{2}\b",                                                     # 2026-08-03
    rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTH}\b(?:,?\s+(?:19|20)\d{{2}}\b)?",     # 3 August 2026
    rf"\b{_MONTH}\.?(?:\s?['’]\s?\d{{2}}|-\d{{2}}|\s+\d{{1,2}},\s+(?:19|20)\d{{2}}|,?\s+(?:19|20)\d{{2}})\b(?!\.\d)",  # Apr'26, Aug 2026
    rf"\b{_PART}?\s?(?:FY|CY)\s?\d{{2,4}}E?(?:[-–/]\d{{2}}E?)?\b(?!\.\d|%)",                # 1QFY27, FY28E, FY26-28E
    rf"\b{_PART}\b",                                                              # 1Q, 2H
    r"\b\d+(?:st|nd|rd|th)\b",                                                    # 2nd
    r"#\d+|^\s*\(?\d{1,2}[.)]\s",                                                 # ranks and list numbering
    r"(?i:\b\d+\s+of\s+(?:the\s+)?\d+\b)",                                        # 2 of 17
    r"(?i:\b\d+\s+(?:reports?|companies|documents?|sources?)\b)",                 # the model's own scope
    r"(?i:\bpages?\s+\d+\b)",                                                    # page 3
]))
_NUMBER = re.compile(r"\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?")
_WORDS = "zero one two three four five six seven eight nine ten eleven twelve".split()
# A claim may print a tool number as is, INR m as INR b or the reverse, or a ratio as a percentage.
_SCALES = (1, 1000, 0.001, 100)
# A percentage, basis points or a multiple is never INR m read as INR b: revenue of 9,384 must not trace "a 9% beat".
_RATE = re.compile(r"\s?(?:%|bps?\b|pp\b|x\b|per\s?cent)")


def number_tokens(text):
    """The numbers printed in a text, as written, leaving out periods (1QFY27), dates and scope counts."""
    return _NUMBER.findall(_NOT_NUMBERS.sub(" ", text))


def untraced(text, tool_numbers):
    """The numbers in `text` that match no tool number after rounding to the printed precision. Sign is ignored."""
    missing, text = [], _NOT_NUMBERS.sub(" ", text)
    for m in _NUMBER.finditer(text):
        plain = m[0].replace(",", "")
        value, half = float(plain), 0.5 * 10 ** -len(plain.partition(".")[2])
        scales = (1, 100) if _RATE.match(text, m.end()) else _SCALES
        if not any(abs(abs(t) * s - value) <= half + 1e-9 for t in tool_numbers for s in scales):
            missing.append(m[0])
    return missing


def collect(result, known_docs, docs, numbers):
    """Add to `docs` and `numbers` every document and number that appears anywhere in one tool result."""
    if isinstance(result, dict):
        # A page number or a chunk id locates a row; it is not a figure, and as a tool number it would trace "3%" or "300bp".
        result = [v for k, v in result.items() if k not in ("page", "id")]
    if isinstance(result, list):
        for item in result:
            collect(item, known_docs, docs, numbers)
    elif isinstance(result, str):
        if result in known_docs:
            docs.add(result)
        numbers.update(float(t.replace(",", "")) for t in number_tokens(result))
        # shortcut: counts spelled out in report text ("eight quarters") are read up to twelve only.
        numbers.update(_WORDS.index(w) for w in re.findall(r"[a-z]+", result.lower()) if w in _WORDS)
    elif isinstance(result, (int, float)) and not isinstance(result, bool):
        numbers.add(result)


def check(claims, docs, numbers):
    """Split claims into (kept, failures). A kept claim has a returned document as source and only traced numbers."""
    kept, failures = [], []
    for claim in claims:
        sources = [s for s in dict.fromkeys(claim["sources"]) if s in docs]
        bad = untraced(claim["text"], numbers)
        if not sources:
            failures.append({"claim": claim["text"], "check": "cites no document returned by your tools"})
        elif bad:
            failures.append({"claim": claim["text"], "check": "numbers not in your tool results: " + ", ".join(bad)})
        else:
            kept.append({"text": claim["text"], "sources": sources})
    return kept, failures


def render(kept, removed, missing):
    lines = [f"{c['text']} " + " ".join(f"[{s}]" for s in c["sources"]) for c in kept]
    if removed:
        lines.append(f"Note: {removed} statement(s) were removed because they could not be traced to a source.")
    if missing:
        lines.append("Not in the corpus: " + missing)
    return "\n".join(lines)


def system_prompt(conn):
    reports = tools.list_reports(conn)
    cols = ["company", "aliases", "sector", "reported_period", "rating", "doc"]
    catalog = "\n".join([" | ".join(cols)] + [" | ".join(str(r[c] or "") for c in cols) for r in reports])
    return RULES.format(rounds=MAX_ROUNDS, catalog=catalog, metrics=", ".join(store.METRICS), kinds=", ".join(store.KINDS),
                        money=", ".join(store.MONEY_UNITS), bases=", ".join(store.BASES))


def run_tool(name, args, conn, coll, data):
    if name == "list_reports":
        return tools.list_reports(conn)
    if name == "list_labels":
        return tools.list_labels(conn, args["company"])
    if name == "query_numbers":
        return tools.query_numbers(args["sql"], data)
    if name == "search_text":
        return tools.search_text(conn, coll, args["query"], args.get("companies"), args.get("k") or 6)
    raise ValueError(f"Unknown tool {name!r}.")


def summary(result):
    if isinstance(result, dict) and "error" in result:
        return f"error: {result['error']}"
    rows = result.get("rows", result.get("chunks", [])) if isinstance(result, dict) else result
    if rows and {"id", "content"} <= rows[0].keys():  # a query may return a content column with no id
        return "chunks: " + ", ".join(str(r["id"]) for r in rows)
    return f"{len(rows)} rows"


def ask(question, data=store.DATA, debug=False):
    conn, coll, client = store.connect(data, readonly=True), store.collection(data), llm.client()
    known_docs = {r["doc"] for r in tools.list_reports(conn)}
    # The marker on the system block caches tools + system; the top-level cache_control caches the growing conversation.
    system = [{"type": "text", "text": system_prompt(conn), "cache_control": {"type": "ephemeral"}}]
    messages = [{"role": "user", "content": [{"type": "text", "text": question}]}]
    docs, numbers, trace, cost = set(), set(), [], 0.0
    answer, nudged, repaired = None, False, False

    def note(entry):
        trace.append(entry)
        if debug:
            print(json.dumps(entry, default=str), file=sys.stderr)

    for rnd in range(1, MAX_ROUNDS + 1):
        if rnd == MAX_ROUNDS:
            messages[-1]["content"].append({"type": "text", "text": "This is the last round: call submit_answer now with what you have."})
        llm.guard()
        resp = client.messages.create(model=MODEL, max_tokens=16000, system=system, tools=TOOLS, messages=messages,
                                      cache_control={"type": "ephemeral"})
        cost += llm.record(MODEL, resp.usage, "ask")
        if resp.stop_reason in ("max_tokens", "refusal"):  # nothing usable came back; whatever was submitted earlier stands
            note({"round": rnd, "check": f"model stopped: {resp.stop_reason}"})
            break
        messages.append({"role": "assistant", "content": resp.content})
        calls = [b for b in resp.content if b.type == "tool_use"]
        if not calls:
            if nudged:
                break
            nudged = True
            messages.append({"role": "user", "content": [{"type": "text", "text": "Plain text is discarded. Call submit_answer now."}]})
            continue
        results = []
        for call in calls:
            if call.name == "submit_answer":
                answer = call.input
                continue
            try:
                result = run_tool(call.name, call.input, conn, coll, data)
            except Exception as e:  # a failing tool is reported to the model, never raised
                result = {"error": f"{type(e).__name__}: {e}"}
            failed = isinstance(result, dict) and "error" in result
            if not failed:
                collect(result, known_docs, docs, numbers)
            note({"round": rnd, "tool": call.name, "args": call.input, "summary": summary(result)})
            results.append({"type": "tool_result", "tool_use_id": call.id, "content": json.dumps(result, default=str), "is_error": failed})
        submits = [c for c in calls if c.name == "submit_answer"]
        if submits:
            failures = check(answer["claims"], docs, numbers)[1]
            for failure in failures:
                note({"round": rnd, **failure})
            if not failures or repaired or answer["refused"] or rnd == MAX_ROUNDS:
                break
            repaired = True
            text = ("Not accepted yet. These claims failed the check; query the numbers or remove them, cite only documents "
                    "your tools returned, then call submit_answer again with the full answer:\n"
                    + "\n".join(f"- {f['claim']} -> {f['check']}" for f in failures))
            results += [{"type": "tool_result", "tool_use_id": c.id, "content": text, "is_error": True} for c in submits]
        messages.append({"role": "user", "content": results})

    kept, failures = check(answer["claims"], docs, numbers) if answer else ([], [])
    missing = answer["missing"].strip() if answer else ""
    if untraced(missing, numbers):  # the missing text is shown to the user, so it is held to the same number check
        missing = ""
    if not kept or answer["refused"]:
        return {"answer": "Not in the corpus: " + (missing or NO_ANSWER), "cited": [], "refused": True, "trace": trace, "cost_usd": cost}
    return {"answer": render(kept, len(failures), missing), "cited": list(dict.fromkeys(s for c in kept for s in c["sources"])),
            "refused": False, "trace": trace, "cost_usd": cost}
