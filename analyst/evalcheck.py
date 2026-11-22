"""Score one answer against one golden row. Pure code: no model judges these."""
import re
from decimal import ROUND_HALF_UP, Decimal


def _norm(s):
    return s.replace(",", "").replace("×", "x").lower()


def _round(d, places):
    return d.quantize(Decimal(1).scaleb(-places), ROUND_HALF_UP)


def _places(s):
    return len(s.partition(".")[2])


def _found(option, text):
    """Words match as whole words. Numbers match by value, so 9.38 and 2.30 satisfy 9,384 (m to b) and 2.3."""
    option = _norm(option)
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(\D*)", option)
    if not m:
        return re.search(r"(?<![a-z])" + re.escape(option) + r"s?(?![a-z])", text) is not None
    want, suffix = m.groups()
    tail = r"\s*" + re.escape(suffix) + r"(?![a-z])" if suffix else ""
    # FY28 / CY26 are period labels, not values
    for got in re.findall(r"(?<![\d.])(?<![fc]y)(\d+(?:\.\d+)?)" + tail, text):
        if _places(got) >= _places(want) and _round(Decimal(got), _places(want)) == Decimal(want):
            return True
        # an INR m figure quoted in INR b; one decimal would match too many stray numbers
        if not suffix and Decimal(want) >= 1000 and _places(got) >= 2 \
                and _round(Decimal(want) / 1000, _places(got)) == Decimal(got):
            return True
    return False


def check(row, answer, cited, refused):
    """Return (passed, reasons). cited = document names the answer cites; refused = it said 'not in the corpus'."""
    want = row["should_refuse"]
    if want == "yes":
        return (refused, [] if refused else ["should have refused"])
    reasons = []
    if refused:
        reasons.append("refused an answerable question")
    text = _norm(answer)
    for group in row["must_include"]:  # each group is any-of, e.g. ["9,384", "9.4"]
        if not any(_found(option, text) for option in group):
            reasons.append(f"missing {' / '.join(group)}")
    have = {d.lower() for d in cited}
    missing = [d for d in row["must_cite"] if d.lower() not in have]
    if missing:
        reasons.append(f"not cited: {', '.join(missing)}")
    return (not reasons, reasons)
