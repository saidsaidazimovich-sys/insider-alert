"""Regression tests for the Navios alert that reported an $846,000,000 purchase.

One real filing exposed four separate defects at once, so it gets its own file.
"""

from decimal import Decimal as D
from pathlib import Path

from insider.form4 import parse_form4
from insider.market import MarketSnapshot
from insider.screen import screen

FIX = Path(__file__).parent / "fixtures"
CFG = {
    "min_value_usd": 250_000,
    "min_pct_of_market_cap": 1.0,
    "allowed_exchanges": [],
    "exclude_tickers": [],
    "alert_when_cap_unknown": True,
}


def doc():
    return parse_form4((FIX / "nmm_price_typo.xml").read_bytes())


def market():
    return MarketSnapshot(
        ticker="NMM", price=D("77.71"), market_cap=D("2200000000"), exchange="NYSE"
    )


def test_the_mistyped_leg_is_dropped_not_believed():
    agg = doc().aggregate_purchase(reference_price=market().price)
    # 1,084 + 1,053 units survive; the 1,131 @ 748119 leg does not.
    assert agg["shares"] == D("2137")
    assert agg["legs"] == 2
    assert len(agg["excluded_legs"]) == 1
    assert agg["excluded_legs"][0]["price"] == D("748119")
    # ~$167k, not $846,289,996
    assert D("167000") < agg["value_usd"] < D("168000")
    assert D("78") < agg["price"] < D("79")


def test_the_filing_no_longer_alerts_at_all():
    """$167k of a $2.2B company fails both thresholds -- as it always should have."""
    d = doc()
    sig, rej = screen(
        d, d.owners[0], "0001193125-26-318541", "https://sec.gov/x",
        "2026-07-28 01:15 UTC", market(), CFG,
    )
    assert sig is None
    assert "< $250,000" in rej.reason


def test_median_fallback_when_no_market_price():
    """Even with no market data the odd leg is still caught, via the median."""
    agg = doc().aggregate_purchase(reference_price=None)
    assert agg["shares"] == D("2137")
    assert len(agg["excluded_legs"]) == 1


def test_rule_10b5_1_is_detected():
    d = doc()
    assert d.aff_10b5_1_flag is True     # dedicated XML element
    assert d.is_10b5_1 is True


def test_10b5_1_detected_from_footnote_without_the_flag():
    from insider.form4 import Form4
    assert Form4(footnotes=["Sold under a Rule 10b5-1 plan."]).is_10b5_1 is True
    assert Form4(footnotes=["Open market purchase."]).is_10b5_1 is False


def test_see_remarks_is_replaced_by_the_real_job_title():
    owner = doc().owners[0]
    assert owner.officer_title == "Chief Executive Officer & Chairwoman of the Board"
    assert "See Remarks" not in owner.role_label
    assert "Chairwoman" in owner.role_label


def test_indirect_ownership_and_date_range_are_reported():
    d = doc()
    agg = d.aggregate_purchase(reference_price=market().price)
    assert agg["all_indirect"] is True
    assert agg["transaction_date"] == "2026-07-24"        # earliest KEPT leg
    assert agg["transaction_date_last"] == "2026-07-27"


def test_a_filing_where_every_price_is_absurd_stays_silent():
    from insider.form4 import Form4, Transaction
    d = Form4(transactions=[
        Transaction("P", "Common", "2026-07-01", D(100), D(999999), "A", None, "D"),
        Transaction("P", "Common", "2026-07-01", D(100), D(888888), "A", None, "D"),
    ])
    assert d.aggregate_purchase(reference_price=D("50")) is None
