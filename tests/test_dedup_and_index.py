"""Regression tests for the '?' alerts and the repeated reports."""

from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path

from insider.edgar import EdgarClient
from insider.form4 import parse_form4
from insider.market import MarketSnapshot
from insider.screen import screen
from insider.state import State

FIX = Path(__file__).parent / "fixtures"
CFG = {
    "min_value_usd": 250_000,
    "min_pct_of_market_cap": 1.0,
    "allowed_exchanges": [],
    "exclude_tickers": [],
    "require_ticker": True,
    "alert_when_cap_unknown": True,
}


def _sig(accession, market=None):
    doc = parse_form4((FIX / "vcig_subscription.xml").read_bytes())
    sig, rej = screen(
        doc, doc.owners[0], accession, "https://sec.gov/x",
        "2026-05-22 08:01 EDT",
        market or MarketSnapshot(ticker="VCIG", price=D("0.51"), market_cap=D("6900000")),
        CFG,
    )
    assert sig is not None, rej
    return sig


def test_same_purchase_filed_twice_has_one_fingerprint():
    """A fund, its GP and its manager each file their own Form 4 for one trade."""
    a = _sig("0001213900-26-060307")
    b = _sig("0001999999-26-000001")      # different document, same purchase
    assert a.accession != b.accession
    assert a.fingerprint == b.fingerprint


def test_fingerprint_separates_genuinely_different_trades():
    doc = parse_form4((FIX / "multi_leg_buy.xml").read_bytes())
    other, _ = screen(
        doc, doc.owners[0], "0001-26-000009", "https://sec.gov/x", "2026-05-08 09:00 EDT",
        MarketSnapshot(ticker="EXEN", price=D("2.40"), market_cap=D("50000000")), CFG,
    )
    assert other.fingerprint != _sig("0001213900-26-060307").fingerprint


def test_state_blocks_the_second_copy(tmp_path):
    st = State.load(tmp_path / "s.json")
    a = _sig("0001213900-26-060307")
    assert st.already_alerted_fp(a.fingerprint) is False
    st.mark_alerted_fp(a.fingerprint)
    assert st.already_alerted_fp(_sig("0001999999-26-000001").fingerprint) is True

    st.save()
    assert State.load(tmp_path / "s.json").already_alerted_fp(a.fingerprint) is True


def test_fetch_failures_retry_then_give_up(tmp_path):
    st = State.load(tmp_path / "s.json")
    acc = "0001-26-000002"
    assert st.note_fetch_failure(acc) == 1
    assert st.note_fetch_failure(acc) == 2
    st.clear_fetch_failure(acc)
    assert st.note_fetch_failure(acc) == 1      # cleared after a success


class _FakeResp:
    def __init__(self, text): self.text = text; self.status_code = 200


def test_daily_index_survives_company_names_containing_double_spaces(monkeypatch):
    """The CIK column shifts when a name has a run of spaces; the trailing
    path does not, so both CIK and accession are read from it."""
    idx = "\n".join([
        "Form Type   Company Name   CIK   Date Filed   File Name",
        "-" * 80,
        "4           HERSHEY TRUST CO  TRUSTEE IN TRUST   938543   2026-07-28   edgar/data/938543/0000950142-26-002183.txt",
        "4/A         Normal Corp   1944831   2026-07-28   edgar/data/1944831/0000950142-26-002184.txt",
        "3           Ignore Me   111   2026-07-28   edgar/data/111/0000000000-26-000001.txt",
    ])
    c = EdgarClient("Test test@example.com")
    monkeypatch.setattr(c, "get", lambda *a, **k: _FakeResp(idx))

    rows = sorted(c.daily_index_form4s(datetime(2026, 7, 28, tzinfo=timezone.utc)),
                  key=lambda e: e.accession)
    assert len(rows) == 2                       # form 3 excluded
    assert rows[0].cik == "938543"
    assert rows[0].accession == "0000950142-26-002183"
    assert rows[0].archive_path == "edgar/data/938543/0000950142-26-002183.txt"
    assert rows[1].is_amendment is True
    # Date-only, so callers must not print it as a clock time.
    assert all(r.filed_at_is_exact is False for r in rows)


def test_untraded_issuer_is_recognisable():
    """Willow Tree Capital Corp: a non-traded BDC, no symbol in the filing.
    With no ticker there is no price, no market cap and nothing to trade."""
    doc = parse_form4(
        b'<?xml version="1.0"?><ownershipDocument><documentType>4</documentType>'
        b"<issuer><issuerCik>0001944831</issuerCik>"
        b"<issuerName>Willow Tree Capital Corp</issuerName>"
        b"<issuerTradingSymbol>NONE</issuerTradingSymbol></issuer>"
        b"<reportingOwner><reportingOwnerId><rptOwnerName>HERSHEY TRUST CO</rptOwnerName>"
        b"</reportingOwnerId><reportingOwnerRelationship><isTenPercentOwner>1"
        b"</isTenPercentOwner></reportingOwnerRelationship></reportingOwner>"
        b"<nonDerivativeTable><nonDerivativeTransaction>"
        b"<transactionDate><value>2026-07-27</value></transactionDate>"
        b"<transactionCoding><transactionCode>P</transactionCode></transactionCoding>"
        b"<transactionAmounts><transactionShares><value>74596</value></transactionShares>"
        b"<transactionPricePerShare><value>16.05</value></transactionPricePerShare>"
        b"<transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>"
        b"</transactionAmounts></nonDerivativeTransaction></nonDerivativeTable>"
        b"</ownershipDocument>"
    )
    assert doc.ticker is None                   # "NONE" is not a symbol
    assert doc.aggregate_purchase()["value_usd"] == D("1197265.80")
