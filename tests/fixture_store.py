"""A small store holding real figures from five of the reports, so the agent tests check real answers."""
from analyst import store

GSK = "MotilalOswal_GLAXO_5765685.pdf"
UPL = "MotilalOswal_UPL_5765696.pdf"
NALCO = "MotilalOswal_NATIONALUM_5765693.pdf"
ESCORTS = "MotilalOswal_ESCORTS_5765676.pdf"
MUTHOOT = "MuthootFinance_MotilalOswal_02082026.pdf"

# doc, company, aliases, sector, cmp, target price (all five are rated Neutral and report 1QFY27)
REPORTS = [
    (GSK, "GSK Pharma", "GlaxoSmithKline Pharmaceuticals,GLAXO,GSK", "Healthcare", 2662, 2870),
    (UPL, "UPL", "UPL Ltd", "Agri", 620, 600),
    (NALCO, "NALCO", "National Aluminium,NATIONALUM", "Metals", 369, 380),
    (ESCORTS, "Escorts Kubota", "Escorts,EKL", "Automobile", 3136, 3348),
    (MUTHOOT, "Muthoot Finance", "Muthoot,MUTH", "Financials", 3120, 2850),
]

# doc: (basis, [(row_label, metric, period, kind, value, unit)])
FACTS = {
    GSK: ("consolidated", [
        ("Net Sales", "revenue", "1QFY27", "actual", 9384, "INR m"),
        ("Net Sales", "revenue", "1QFY27", "broker_estimate", 8788, "INR m"),
        ("Net Sales: YoY Change (%)", "revenue_growth_yoy_pct", "1QFY27", "actual", 16.5, "%"),
        ("Net Sales", "revenue", "2QFY27", "actual", 11275, "INR m"),  # stored as a forecast: the period is after 1QFY27
    ]),
    UPL: ("consolidated", [
        ("Net Sales", "revenue", "1QFY27", "actual", 101.8, "INR b"),
        ("Net Sales: YoY Change (%)", "revenue_growth_yoy_pct", "1QFY27", "actual", 10.5, "%"),
    ]),
    NALCO: ("consolidated", [
        ("Net Sales", "revenue", "1QFY27", "actual", 53024, "INR m"),
        ("Net Sales: YoY Change (%)", "revenue_growth_yoy_pct", "1QFY27", "actual", 39.3, "%"),
    ]),
    ESCORTS: ("standalone", [
        ("Net Sales", "revenue", "1QFY27", "actual", 31789, "INR m"),
        ("Net Sales: YoY Change (%)", "revenue_growth_yoy_pct", "1QFY27", "actual", 28.0, "%"),
        ("Gross Margin (%)", "gross_margin_pct", "1QFY27", "actual", 27.2, "%"),
    ]),
    # A lender: no revenue line, so no standard metric name on either row.
    MUTHOOT: ("standalone", [
        ("Total operating income", None, "1QFY27", "actual", 75889, "INR m"),
        ("Total operating income: YoY Growth (%)", None, "1QFY27", "actual", 33.1, "%"),
    ]),
}

# doc, section, content
CHUNKS = [
    (ESCORTS, "Margins under pressure", "Gross margin fell about 370bp YoY to 27.2% in 1QFY27, primarily because of "
     "raw-material cost inflation. Management expects margin pressure to continue in 2Q."),
    (GSK, "Strong quarter", "Revenue grew 16.5% YoY in 1QFY27. This was the first high-teen growth after eight quarters."),
    (NALCO, "Valuation and view", "We reiterate our Neutral rating with a TP of INR380, valuing the stock at 6.5x FY28E EV/EBITDA."),
]


def build(data):
    """Create the fixture database and vector index under the directory `data` (a pathlib.Path)."""
    conn, coll = store.connect(data), store.collection(data)
    company = {}
    for doc, name, aliases, sector, cmp, target in REPORTS:
        company[doc] = name
        store.add_report(conn, {"doc": doc, "company": name, "aliases": aliases, "sector": sector,
                                "report_type": "results update", "reported_period": "1QFY27", "rating": "Neutral",
                                "cmp": cmp, "target_price": target})
    for doc, (basis, rows) in FACTS.items():
        store.add_facts(conn, company[doc], doc, "1QFY27", [
            {"page": 1, "source_table": "Quarterly performance", "row_label": label, "metric": metric,
             "period": period, "kind": kind, "value": value, "unit": unit, "basis": basis}
            for label, metric, period, kind, value, unit in rows])
    store.add_chunks(conn, coll, [
        {"id": f"{doc}:p1:c{i}", "company": company[doc], "doc": doc, "page": 1, "section": section,
         "kind": "text", "content": content, "search_text": f"{company[doc]}. {section}. {content}"}
        for i, (doc, section, content) in enumerate(CHUNKS)])
    conn.commit()
    conn.close()
