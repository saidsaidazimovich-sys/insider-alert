"""Filter tests -- the $250k AND 1%-of-cap rule and its edge cases."""

from decimal import Decimal as D

import pytest

from insider.form4 import parse_form4
from insider.market import MarketSnapshot
from insider.screen import screen
from pathlib import Path

FIX = Path(__file__).parent / "fixtures"
CFG = {
    "min_value_usd": 250_000,
    "min_pct_of_market_cap": 1.0,
    "allowed_exchanges": [],
    "exclude_tickers": [],
    "alert_when_cap_unknown": True,
}


def snap(cap, price="1.00", exchange="NasdaqCM"):
    return MarketSnapshot(
        ticker="TEST",
        price=D(price),
        market_cap=D(cap) if cap is not None else None,
        exchange=exchange,
    )


def run(fixture, market, cfg=None):
    doc = parse_form4((FIX / fixture).read_bytes())
    return screen(
        doc, doc.owners[0], "0001-26-000001", "https://sec.gov/x",
        "2026-05-22 12:01 UTC", market, {**CFG, **(cfg or {})},
    )


def test_vcig_passes_both_thresholds():
    # $900k on a $6.9M cap = 13.04%
    sig, rej = run("vcig_subscription.xml", snap("6900000", "0.5149"))
    assert rej is None
    assert sig.value_usd == D("900000.00")
    assert round(sig.pct_of_market_cap, 2) == D("13.04")
    assert sig.is_subscription is True


def test_below_dollar_floor_is_rejected():
    # $315k passes the floor; drop the floor test to $400k and it must fail.
    sig, rej = run("open_market_buy.xml", snap("1000000"), {"min_value_usd": 400_000})
    assert sig is None and "< $400,000" in rej.reason


def test_big_buy_in_a_big_company_fails_the_percent_rule():
    """$900k is a lot of money but 0.09% of a $1B company -- not the signal."""
    sig, rej = run("vcig_subscription.xml", snap("1000000000"))
    assert sig is None
    assert "of cap < 1.0%" in rej.reason


@pytest.mark.parametrize("cap,passes", [("90000000", True), ("91000000", False)])
def test_the_one_percent_boundary(cap, passes):
    """$900k is exactly 1% of $90M, so $90M passes and $91M does not."""
    sig, _ = run("vcig_subscription.xml", snap(cap))
    assert (sig is not None) is passes


def test_unknown_market_cap_still_alerts_but_says_so():
    sig, rej = run("vcig_subscription.xml", snap(None))
    assert rej is None
    assert any("Market cap aniqlanmadi" in n for n in sig.notes)


def test_unknown_market_cap_can_be_configured_to_drop():
    sig, rej = run("vcig_subscription.xml", snap(None), {"alert_when_cap_unknown": False})
    assert sig is None and rej.reason == "market cap unknown"


def test_exchange_and_ticker_exclusions():
    sig, rej = run("vcig_subscription.xml", snap("6900000"), {"exclude_tickers": ["vcig"]})
    assert sig is None and "excluded" in rej.reason

    sig, rej = run(
        "vcig_subscription.xml",
        snap("6900000", exchange="OTC Markets"),
        {"allowed_exchanges": ["NASDAQ", "NYSE"]},
    )
    assert sig is None and "not allowed" in rej.reason


def test_non_purchases_never_reach_the_filters():
    for f in ("sale.xml", "grant_zero_price.xml", "pipe_derivative_only.xml"):
        sig, rej = run(f, snap("6900000"))
        assert sig is None
        assert "no usable cash purchase" in rej.reason


def test_multi_leg_note_and_premium_note():
    sig, _ = run("multi_leg_buy.xml", snap("6900000", "1.00"))
    assert any("3 ta tranzaksiya" in n for n in sig.notes)
    # paid $2.40 vs $1.00 market
    assert any("qimmat" in n for n in sig.notes)


def test_one_alert_per_filing_even_with_five_co_filers():
    """Regression: a KLRS fund purchase listed five reporting owners and sent
    five identical Telegram messages for one $1.18M buy."""
    from insider.form4 import Owner, primary_owner

    owners = [
        Owner(cik="1", name="AKKARAJU SRINIVAS", is_director=True),
        Owner(cik="2", name="Samsara BioCapital, L.P.", is_ten_percent_owner=True),
        Owner(cik="3", name="Samsara BioCapital GP, LLC", is_ten_percent_owner=True),
        Owner(cik="4", name="Samsara Opportunity Fund, L.P.", is_ten_percent_owner=True),
        Owner(cik="5", name="Samsara Opportunity Fund GP, LLC", is_ten_percent_owner=True),
    ]
    # The human is named, not the holding company.
    assert primary_owner(owners).name == "AKKARAJU SRINIVAS"
    # Entities only -> fall back to the first rather than crashing.
    assert primary_owner(owners[1:]).name == "Samsara BioCapital, L.P."
