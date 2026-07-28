"""
Screening: decide whether a parsed purchase deserves an alert.

Said's two thresholds are combined with AND, as he specified:
    value >= min_value_usd  AND  value >= min_pct_of_market_cap % of cap

Worth knowing what that implies: because both must hold, the cap ceiling is
value x (100 / min_pct). At $250k and 1% only companies under $25M can ever
qualify; a $1M buy needs a cap under $100M. This is a nano/micro-cap screen by
construction, which matches the VCIG-shaped setups he is looking for.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from .form4 import Form4, Owner
from .market import MarketSnapshot

log = logging.getLogger(__name__)


@dataclass
class Signal:
    accession: str
    filing_url: str
    filed_at: str
    is_amendment: bool

    ticker: str | None
    issuer_name: str | None
    owner_name: str | None
    role_label: str

    shares: Decimal
    price: Decimal
    value_usd: Decimal
    transaction_date: str | None
    shares_owned_after: Decimal | None
    legs: int

    market: MarketSnapshot | None
    pct_of_market_cap: Decimal | None
    is_subscription: bool
    subscription_evidence: str | None
    is_10b5_1: bool
    transaction_date_last: str | None
    notes: list[str]


@dataclass
class Rejection:
    accession: str
    reason: str


def _pct_of_cap(value: Decimal, cap: Decimal | None) -> Decimal | None:
    if cap is None or cap <= 0:
        return None
    return value / cap * Decimal(100)


def screen(
    doc: Form4,
    owner: Owner,
    accession: str,
    filing_url: str,
    filed_at: str,
    market: MarketSnapshot | None,
    cfg: dict,
) -> tuple[Signal | None, Rejection | None]:
    """Return (signal, None) if it passes, else (None, rejection)."""
    # Hand the parser a trustworthy reference price so it can throw out legs
    # with a mistyped price before any arithmetic depends on them.
    agg = doc.aggregate_purchase(reference_price=market.price if market else None)
    if agg is None:
        return None, Rejection(
            accession, "no usable cash purchase of common stock (or every price looked wrong)"
        )

    value: Decimal = agg["value_usd"]
    notes: list[str] = []

    min_value = Decimal(str(cfg.get("min_value_usd", 250_000)))
    if value < min_value:
        return None, Rejection(accession, f"value ${value:,.0f} < ${min_value:,.0f}")

    if doc.ticker and doc.ticker.upper() in {
        t.upper() for t in cfg.get("exclude_tickers", []) or []
    }:
        return None, Rejection(accession, f"{doc.ticker} is excluded")

    cap = market.market_cap if market else None
    pct = _pct_of_cap(value, cap)
    min_pct = Decimal(str(cfg.get("min_pct_of_market_cap", 1.0)))

    if pct is None:
        # Deliberately do not drop it -- see market.py docstring.
        if cfg.get("alert_when_cap_unknown", True):
            notes.append("Market cap aniqlanmadi — 1% sharti tekshirilmadi")
        else:
            return None, Rejection(accession, "market cap unknown")
    elif pct < min_pct:
        return None, Rejection(
            accession, f"{pct:.2f}% of cap < {min_pct}% (cap ${cap:,.0f})"
        )

    allowed = cfg.get("allowed_exchanges") or []
    if allowed and market and market.exchange:
        ex = market.exchange.upper()
        if not any(a.upper() in ex for a in allowed):
            return None, Rejection(accession, f"exchange {market.exchange} not allowed")

    if agg["legs"] > 1:
        notes.append(f"{agg['legs']} ta tranzaksiya birlashtirildi (vaznli o'rtacha narx)")
    for bad in agg["excluded_legs"]:
        notes.append(
            f"\u26a0\ufe0f Filing'da ishonchsiz narx bor edi ({bad['shares']:,.0f} \u00d7 "
            f"${bad['price']:,.4f}) \u2014 hisobga olinmadi, SEC hujjatini o'zingiz tekshiring"
        )
    if agg["all_indirect"]:
        notes.append("Bilvosita egalik (shaxsan emas, tuzilma orqali)")
    if agg["derivative_purchase_legs"]:
        notes.append(
            f"{agg['derivative_purchase_legs']} ta derivativ xarid ham bor "
            f"(hisobga olinmadi)"
        )
    if doc.is_amendment:
        notes.append("Bu tuzatilgan filing (Form 4/A)")
    if market and market.price and market.price > 0 and agg["price"] > market.price:
        prem = (agg["price"] - market.price) / market.price * 100
        # Beyond this the number is a data problem, not a premium worth reporting.
        if prem <= 500:
            notes.append(f"Xarid narxi joriy bozordan {prem:.1f}% qimmat")

    return (
        Signal(
            accession=accession,
            filing_url=filing_url,
            filed_at=filed_at,
            is_amendment=doc.is_amendment,
            ticker=doc.ticker,
            issuer_name=doc.issuer_name,
            owner_name=owner.name,
            role_label=owner.role_label,
            shares=agg["shares"],
            price=agg["price"],
            value_usd=value,
            transaction_date=agg["transaction_date"],
            shares_owned_after=agg["shares_owned_after"],
            legs=agg["legs"],
            market=market,
            pct_of_market_cap=pct,
            is_subscription=doc.is_subscription,
            subscription_evidence=doc.subscription_evidence,
            is_10b5_1=doc.is_10b5_1,
            transaction_date_last=agg["transaction_date_last"],
            notes=notes,
        ),
        None,
    )
