import json
import re
from pathlib import Path

import pymupdf
import pytest

from analyst import pdf

DOCS = Path(__file__).parent.parent / "docs"
CARDS = json.loads((Path(__file__).parent / "cards.json").read_text())
PDFS = sorted(DOCS.glob("*.pdf"))


def page_text(name, page):
    return dict(pdf.content_pages(DOCS / name))[page]


def test_17_reports_and_cards():
    assert len(PDFS) == 17
    assert sorted(c["doc"] for c in CARDS) == [p.name for p in PDFS]


@pytest.mark.parametrize("path", PDFS, ids=lambda p: p.name)
def test_content_pages_stop_before_disclosures(path):
    with pymupdf.open(path) as d:
        total = len(d)
    pages = pdf.content_pages(path)
    assert 4 <= len(pages) < total
    assert [n for n, _ in pages] == list(range(1, len(pages) + 1))
    assert not any(pdf.DISCLAIMER_START in t for _, t in pages)


def test_numbers_formats():
    got = pdf.numbers("Rs 2,30,790 and 16.5% grew; EBITDA 4,113. Loss (1,234) and -5.2 then 13.80")
    assert {230790.0, 16.5, 4113.0, 1234.0, 5.2, 13.8} <= got


def test_numbers_trailing_zero_and_sign():
    assert pdf.on_page(13.8, "margin 13.80 %")
    assert pdf.on_page(-1234, "loss (1,234)")
    assert not pdf.on_page(9385, "revenue 9,384")


def test_on_page_real_reports():
    glaxo = " ".join(t for _, t in pdf.content_pages(DOCS / "MotilalOswal_GLAXO_5765685.pdf"))
    assert pdf.on_page(9384, glaxo) and pdf.on_page(8788, glaxo)
    assert not pdf.on_page(9385, glaxo)
    nalco = " ".join(t for _, t in pdf.content_pages(DOCS / "MotilalOswal_NATIONALUM_5765693.pdf"))
    assert pdf.on_page(82882, nalco)
    assert pdf.on_page(5522, page_text("apl-financial-statement.pdf", 1))


@pytest.mark.parametrize("card", CARDS, ids=lambda c: c["doc"])
def test_card_cmp_and_tp_printed_on_page_1(card):
    t = page_text(card["doc"], 1)
    cmp_ = re.search(r"CMP:\s*INR\s*([\d,]+)", t)
    tp = re.search(r"TP:\s*INR\s*([\d,]+)", t)
    assert cmp_ and float(cmp_[1].replace(",", "")) == card["cmp"]
    assert tp and float(tp[1].replace(",", "")) == card["target_price"]
    assert pdf.on_page(card["cmp"], t) and pdf.on_page(card["target_price"], t)
    assert re.search(r"Sector:\s*" + re.escape(card["sector"]), t)


def test_blank_notes_pages_are_dropped():
    # five reports end with an empty "N O T E S" page; sending it to the vision model is a wasted call
    for path in PDFS:
        assert not [n for n, t in pdf.content_pages(path) if "N O T E S" in t], path.name
    assert len(pdf.content_pages(DOCS / "ITC_MotilalOswal_02082026.pdf")) == 9
    assert sum(len(pdf.content_pages(p)) for p in PDFS) == 147


def test_on_page_non_number_is_false_not_a_crash():
    assert not pdf.on_page(None, "revenue 9,384")
    assert not pdf.on_page("N/M", "revenue 9,384")
    assert pdf.on_page("9384", "revenue 9,384")


@pytest.mark.parametrize("card", CARDS, ids=lambda c: c["doc"])
def test_card_rating_and_report_type_on_page_1(card):
    t = page_text(card["doc"], 1)
    assert re.search(r"TP:\s*INR\s*[\d,]+\s*\([^)]*\)\s*" + card["rating"] + r"\b", t)
    kind = "Company Update" if card["reported_period"] == "FY26" else card["reported_period"] + r"\s+Results? Update"
    assert re.search(kind + r"\s*\|\s*Sector:", t)
