"""Parser tests. Every case here is a way a real filing has broken naive parsers."""

from decimal import Decimal
from pathlib import Path

import pytest

from insider.form4 import parse_form4

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return parse_form4((FIX / name).read_bytes())


def test_vcig_subscription_is_a_purchase_but_flagged():
    doc = load("vcig_subscription.xml")
    assert doc.ticker == "VCIG"
    assert doc.issuer_name == "VCI Global Ltd"
    assert doc.issuer_cik == "1930510"
    assert len(doc.owners) == 1

    owner = doc.owners[0]
    assert owner.name == "Hoo Voon Him"
    assert owner.is_officer and owner.is_director and owner.is_ten_percent_owner
    assert "Chief Executive Officer" in owner.role_label
    assert "10% Owner" in owner.role_label

    agg = doc.aggregate_purchase()
    assert agg["shares"] == Decimal("1200000")
    assert agg["price"] == Decimal("0.75")
    assert agg["value_usd"] == Decimal("900000.00")
    assert agg["shares_owned_after"] == Decimal("1571398")

    # The whole point: code P, but the shares came from the company.
    assert doc.is_subscription is True
    # Whichever phrase hits first, we keep it as evidence for the alert text.
    assert doc.subscription_evidence.lower() in {"subscription", "from the issuer"}


def test_open_market_buy_is_not_flagged_as_subscription():
    doc = load("open_market_buy.xml")
    assert doc.is_subscription is False
    agg = doc.aggregate_purchase()
    assert agg["value_usd"] == Decimal("315000.00")
    assert doc.owners[0].role_label == "Chief Financial Officer"


@pytest.mark.parametrize("name", ["sale.xml", "grant_zero_price.xml", "option_exercise.xml"])
def test_non_purchases_produce_nothing(name):
    """Sales, free grants and option exercises must never reach an alert."""
    doc = load(name)
    assert doc.purchases == []
    assert doc.aggregate_purchase() is None


def test_grant_is_rejected_on_price_even_though_shares_were_acquired():
    doc = load("grant_zero_price.xml")
    txn = doc.transactions[0]
    assert txn.code == "A"
    assert txn.acquired_disposed == "A"      # acquired...
    assert txn.price == Decimal("0")         # ...but paid nothing
    assert txn.is_purchase is False


def test_multi_leg_buy_collapses_with_volume_weighted_price():
    doc = load("multi_leg_buy.xml")
    assert len(doc.purchases) == 3
    agg = doc.aggregate_purchase()
    assert agg["legs"] == 3
    assert agg["shares"] == Decimal("250000")
    # 100k*2.00 + 100k*2.50 + 50k*3.00 = 600,000 over 250,000 shares = 2.40
    assert agg["value_usd"] == Decimal("600000.00")
    assert agg["price"] == Decimal("2.40")
    # Latest reported holding wins, not the first.
    assert agg["shares_owned_after"] == Decimal("1250000")


def test_malformed_filing_degrades_instead_of_crashing():
    doc = load("malformed.xml")
    assert doc.is_amendment is True          # 4/A
    assert doc.ticker is None                # "NONE" is not a ticker
    # A bare "&" is invalid XML; recover mode drops the entity rather than
    # failing the whole filing. Mangled name is acceptable, a crash is not.
    assert "Sons Holdings" in doc.issuer_name
    # Price is only a footnote reference, so there is no verifiable cash amount.
    assert doc.transactions[0].price is None
    assert doc.transactions[0].is_purchase is False
    assert doc.aggregate_purchase() is None


def test_empty_and_garbage_input_never_raise():
    for junk in (b"", b"not xml at all", b"<ownershipDocument>"):
        doc = parse_form4(junk)
        assert doc.aggregate_purchase() is None
        assert doc.warnings


def test_subscription_detection_catches_the_common_phrasings():
    from insider.form4 import Form4

    for phrase in [
        "shares issued in a private placement",
        "purchased directly from the Issuer",
        "pursuant to a securities purchase agreement",
        "acquired in the PIPE transaction",
        "newly-issued ordinary shares",
        "the reporting person subscribed for shares",
    ]:
        doc = Form4(footnotes=[phrase])
        assert doc.is_subscription is True, phrase

    assert Form4(footnotes=["Open market purchase in multiple trades."]).is_subscription is False


def test_pipe_financing_on_derivatives_only_does_not_alert():
    """Real regression: code P on preferred stock + warrants under an SPA.

    The insider paid cash, but for a structured private instrument, not for
    stock on the open market. Alerting on this would be a false positive.
    """
    doc = load("pipe_derivative_only.xml")
    assert doc.transactions, "the legs must still be parsed"
    assert doc.derivative_purchases, "and recognised as derivative purchases"
    assert doc.purchases == []           # nothing in common stock
    assert doc.aggregate_purchase() is None
    # The SPA language is caught, so even if a future config lets derivatives
    # through, the alert would carry the flag.
    assert doc.is_subscription is True


def test_derivative_legs_never_inflate_a_real_share_purchase():
    from insider.form4 import Form4, Transaction
    from decimal import Decimal as D

    doc = Form4(
        transactions=[
            Transaction("P", "Common Stock", "2026-05-20", D(100000), D(2), "A", D(500000), "D"),
            Transaction("P", "Warrant", "2026-05-20", D(9999), D(500), "A", None, "D",
                        is_derivative=True),
        ]
    )
    agg = doc.aggregate_purchase()
    assert agg["value_usd"] == D(200000)          # not 200,000 + 4,999,500
    assert agg["derivative_purchase_legs"] == 1   # but we know it was there
