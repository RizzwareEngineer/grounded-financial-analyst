"""Raw page text from the PDFs. Used for narrative and to check every number the vision model reports."""
import re

import pymupdf

# Every report ends with the broker's rating legend and disclosures; nothing after this is content.
DISCLAIMER_START = "Explanation of Investment Rating"


def content_pages(path):
    """[(page_number, text)] for the pages before the disclosures, 1-indexed."""
    pages = []
    with pymupdf.open(path) as doc:
        for i, page in enumerate(doc, start=1):
            text = page.get_text()
            if DISCLAIMER_START in text:
                break
            if "N O T E S" in text:  # blank notes page: nothing to read, not worth a vision call
                continue
            pages.append((i, text))
    return pages


def numbers(text):
    """Every number printed in the text, as floats: '2,30,790' -> 230790.0, '16.5%' -> 16.5."""
    out = set()
    for token in re.findall(r"\d[\d,]*\.?\d*", text):
        try:
            out.add(float(token.replace(",", "")))
        except ValueError:
            pass
    return out


def on_page(value, page_text):
    """True if a number the model reported is literally printed on the page (sign ignored)."""
    try:
        return abs(float(value)) in numbers(page_text)
    except (TypeError, ValueError):  # the model sent null or "N/M"
        return False
