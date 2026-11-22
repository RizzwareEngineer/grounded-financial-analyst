from analyst.evalcheck import check


def row(**kw):
    base = {"should_refuse": "no", "must_include": [["9,384", "9.4"], ["16.5"]], "must_cite": ["A.pdf", "B.pdf"]}
    return {**base, **kw}


GOOD = "Revenue INR9,384m, up 16.5% YoY."


def test_refuse_yes():
    r = row(should_refuse="yes")
    assert check(r, "not in the corpus", [], True)[0]
    assert not check(r, "HDFC is great", ["A.pdf"], False)[0]


def test_refuse_no_answerable():
    assert check(row(), GOOD, ["A.pdf", "B.pdf"], False) == (True, [])
    assert not check(row(), "not in the corpus", [], True)[0]


def test_refuse_partial():
    r = row(should_refuse="partial")
    assert check(r, GOOD, ["A.pdf", "B.pdf"], False)[0]
    assert not check(r, "not in the corpus", [], True)[0]


def test_any_of_group():
    assert check(row(), "about 9.4b, up 16.5%", ["A.pdf", "B.pdf"], False)[0]


def test_every_group_required():
    passed, reasons = check(row(), "Revenue INR9,384m", ["A.pdf", "B.pdf"], False)
    assert not passed and reasons == ["missing 16.5"]


def test_must_cite_subset():
    assert check(row(), GOOD, ["A.pdf", "B.pdf", "C.pdf"], False)[0]
    passed, reasons = check(row(), GOOD, ["A.pdf"], False)
    assert not passed and "B.pdf" in reasons[0]


def test_cite_case_insensitive():
    assert check(row(), GOOD, ["a.PDF", "b.pdf"], False)[0]


def test_number_boundaries():
    r = row(must_include=[["28"]], must_cite=[])
    for ok in ("28%", "up 28.0%", "growth of 28"):
        assert check(r, ok, [], False)[0], ok
    for bad in ("128", "2.28", "280"):
        assert not check(r, bad, [], False)[0], bad
    assert check(row(must_include=[["39"]], must_cite=[]), "39.3%", [], False)[0]


def test_thousands_separators_and_words():
    r = row(must_include=[["9,384"], ["Neutral"]], must_cite=[])
    assert check(r, "neutral, 9384 total", [], False)[0]
    assert check(r, "NEUTRAL; 9,384", [], False)[0]


def one(option, answer):
    return check(row(must_include=[[option]], must_cite=[]), answer, [], False)[0]


def test_rounded_and_scaled_numbers():
    assert check(row(must_cite=[]), "Revenue was INR9.38b, up 16.50% YoY", [], False)[0]
    assert one("2.3", "2.30x") and one("9,384", "INR 9,384.0 m")
    assert not one("39", "39.7%")  # rounds to 40
    assert not one("16.5", "about 17%")  # less precise than the row
    assert not one("1,146", "1.1x book-to-bill")  # one decimal is too loose for a m-to-b match


def test_fiscal_year_labels_are_not_values():
    assert not one("28", "NALCO grew 39% in 1QFY27; FY28E EBITDA is higher")
    assert one("28", "up 28% in 1QFY27")


def test_suffixed_numbers():
    assert one("6.5x", "6.5× EV/EBITDA") and one("6.5x", "6.5 x FY28E")
    assert one("10%", "roughly 10 %") and one("6%", "6.0%")
    assert not one("6%", "16%") and not one("35x", "135x")


def test_words_are_whole_words():
    assert not one("Divi", "the dividend rose")
    assert not one("ITC", "which may switch") and not one("UPL", "a couple of uplifts")
    assert one("Divi", "Divi's led") and one("Divi", "Divis Laboratories") and one("UPL", "UPL's revenue")
    assert one("Restaurant Brands", "restaurant brands asia")


def test_empty_answer_fails():
    assert not check(row(), "", [], False)[0]


def test_refusal_that_lists_sources_still_counts_as_refusal():
    assert check(row(should_refuse="yes"), "Not in the corpus.", ["A.pdf"], True)[0]
