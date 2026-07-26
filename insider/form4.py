"""
Form 4 (ownershipDocument) XML parser.

Design rules:
  * Never raise on malformed / non-standard XML. Return what we could read and
    record the problem in `.warnings`. A single bad filing must not kill the loop.
  * Only transactionCode "P" with acquiredDisposedCode "A" and a real
    (non-zero) price counts as an insider PURCHASE.
  * Code "P" covers BOTH open-market buys AND buying newly issued shares
    straight from the company (subscription / private placement / PIPE).
    Those are economically different (the company gets the cash, the share
    count grows = dilution), so we detect them from the footnotes and flag
    them instead of silently mixing them in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from lxml import etree

# --- transaction code meanings (SEC Table I / II code list) -------------------
# We keep the full map so log lines are readable, not just so we can filter.
CODE_MEANINGS = {
    "P": "Open-market or private purchase",
    "S": "Open-market or private sale",
    "A": "Grant/award from the issuer",
    "D": "Disposition back to the issuer",
    "F": "Shares withheld to pay tax",
    "M": "Option exercise / conversion",
    "X": "In-the-money option exercise",
    "C": "Conversion of derivative",
    "G": "Bona fide gift",
    "V": "Voluntary early report",
    "J": "Other (see footnotes)",
    "K": "Equity swap",
    "U": "Tender of shares",
    "I": "Discretionary transaction",
    "E": "Expiration of short position",
    "H": "Expiration (long)",
    "L": "Small acquisition",
    "W": "Will / inheritance",
    "Z": "Voting trust deposit/withdrawal",
}

# Only this one is a purchase for our purposes.
PURCHASE_CODE = "P"

# Footnote phrases that mean "bought straight from the company", i.e. new shares
# were issued and the cash went to the treasury -- not an open-market buy.
SUBSCRIPTION_PATTERNS = [
    r"subscription",
    r"subscribed",
    r"private placement",
    r"\bPIPE\b",
    r"directly from the (?:issuer|company)",
    r"from the issuer",
    r"newly[- ]issued",
    r"securities purchase agreement",
    r"share purchase agreement",
    r"stock purchase agreement",
]
_SUBSCRIPTION_RE = re.compile("|".join(SUBSCRIPTION_PATTERNS), re.IGNORECASE)


def _text(node, path: str) -> str | None:
    """Read <path>/value, falling back to <path>'s own text.

    SEC wraps most leaf values in <value>, but some old filings and some
    filing agents skip the wrapper, and some elements carry only a
    <footnoteId> with no value at all.
    """
    if node is None:
        return None
    el = node.find(f"{path}/value")
    if el is None:
        el = node.find(path)
    if el is None or el.text is None:
        return None
    out = el.text.strip()
    return out or None


def _decimal(node, path: str) -> Decimal | None:
    raw = _text(node, path)
    if raw is None:
        return None
    # Filings occasionally contain "1,200,000" or "$0.75" or trailing notes.
    cleaned = re.sub(r"[,$\s]", "", raw)
    try:
        return Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None


def _flag(node, path: str) -> bool:
    raw = _text(node, path)
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "y", "yes"}


@dataclass
class Transaction:
    code: str | None
    security_title: str | None
    transaction_date: str | None
    shares: Decimal | None
    price: Decimal | None
    acquired_disposed: str | None          # "A" acquired / "D" disposed
    shares_owned_after: Decimal | None
    direct_or_indirect: str | None         # "D" direct / "I" indirect
    is_derivative: bool = False

    @property
    def value_usd(self) -> Decimal | None:
        if self.shares is None or self.price is None:
            return None
        return self.shares * self.price

    @property
    def is_purchase(self) -> bool:
        """A real cash purchase: code P, acquired, and an actual price paid.

        A price of 0 means nothing was paid, which is a grant dressed up as
        something else -- never treat it as conviction buying.
        """
        return (
            self.code == PURCHASE_CODE
            and (self.acquired_disposed or "").upper() == "A"
            and self.price is not None
            and self.price > 0
            and self.shares is not None
            and self.shares > 0
        )


@dataclass
class Owner:
    cik: str | None
    name: str | None
    is_director: bool = False
    is_officer: bool = False
    is_ten_percent_owner: bool = False
    is_other: bool = False
    officer_title: str | None = None

    @property
    def roles(self) -> list[str]:
        out = []
        if self.is_officer:
            out.append(self.officer_title.strip() if self.officer_title else "Officer")
        if self.is_director:
            out.append("Director")
        if self.is_ten_percent_owner:
            out.append("10% Owner")
        if self.is_other and not out:
            out.append("Other")
        return out

    @property
    def role_label(self) -> str:
        r = self.roles
        if not r:
            return "Insider"
        return r[0] if len(r) == 1 else ", ".join(r[:-1]) + " va " + r[-1]


def primary_owner(owners: list["Owner"]) -> "Owner":
    """Pick the one owner an alert should name.

    Fund purchases are filed jointly: the individual manager plus the fund,
    its GP, the opportunity fund and that fund's GP can all appear as
    reporting owners on ONE $1.2M purchase. Prefer a natural person (someone
    flagged as officer or director) so the alert names a human rather than
    "Samsara BioCapital GP, LLC".
    """
    for o in owners:
        if o.is_officer or o.is_director:
            return o
    return owners[0]


@dataclass
class Form4:
    document_type: str | None = None          # "4" or "4/A"
    period_of_report: str | None = None
    issuer_cik: str | None = None
    issuer_name: str | None = None
    ticker: str | None = None
    owners: list[Owner] = field(default_factory=list)
    transactions: list[Transaction] = field(default_factory=list)
    footnotes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # --- derived -------------------------------------------------------------
    @property
    def is_amendment(self) -> bool:
        return bool(self.document_type and self.document_type.strip().endswith("/A"))

    @property
    def footnote_text(self) -> str:
        return " ".join(self.footnotes)

    @property
    def is_subscription(self) -> bool:
        """True when footnotes say the shares came from the issuer, not the market."""
        return bool(_SUBSCRIPTION_RE.search(self.footnote_text))

    @property
    def subscription_evidence(self) -> str | None:
        m = _SUBSCRIPTION_RE.search(self.footnote_text)
        return m.group(0) if m else None

    @property
    def purchases(self) -> list[Transaction]:
        """Cash purchases of the actual common stock.

        Derivative legs are deliberately excluded. A real filing (SHF Holdings,
        accession 0001493152-26-022075) reports code P on "Series B Convertible
        Preferred Stock" at $800 a unit plus warrants at $0 -- an insider
        funding the company through a Securities Purchase Agreement. That is
        financing, not someone buying stock on the open market, and the unit
        counts are not comparable to share counts either. Kept separately in
        `derivative_purchases` so it is visible rather than silently dropped.
        """
        return [t for t in self.transactions if t.is_purchase and not t.is_derivative]

    @property
    def derivative_purchases(self) -> list[Transaction]:
        return [t for t in self.transactions if t.is_purchase and t.is_derivative]

    def aggregate_purchase(self) -> dict | None:
        """Collapse every purchase leg in this filing into one buy.

        One Form 4 often reports the same buy split across several prices
        (e.g. broker fills). Reporting them separately would spam alerts and
        understate the size, so we sum the shares and volume-weight the price.
        """
        legs = self.purchases
        if not legs:
            return None
        total_shares = sum(t.shares for t in legs)
        total_value = sum(t.value_usd for t in legs)
        vwap = (total_value / total_shares) if total_shares else None
        # Post-transaction holding: take the last reported non-derivative leg,
        # which reflects the position after the whole filing.
        after = None
        for t in legs:
            if t.shares_owned_after is not None:
                after = t.shares_owned_after
        return {
            "shares": total_shares,
            "price": vwap,
            "value_usd": total_value,
            "legs": len(legs),
            "transaction_date": min(
                (t.transaction_date for t in legs if t.transaction_date), default=None
            ),
            "shares_owned_after": after,
            "derivative_purchase_legs": len(self.derivative_purchases),
        }


def _parse_transaction(node, is_derivative: bool) -> Transaction:
    amounts = node.find("transactionAmounts")
    post = node.find("postTransactionAmounts")
    coding = node.find("transactionCoding")
    nature = node.find("ownershipNature")

    code = None
    if coding is not None:
        code = _text(coding, "transactionCode") or (
            coding.findtext("transactionCode") or ""
        ).strip() or None

    return Transaction(
        code=code.upper() if code else None,
        security_title=_text(node, "securityTitle"),
        transaction_date=_text(node, "transactionDate"),
        shares=_decimal(amounts, "transactionShares") if amounts is not None else None,
        price=_decimal(amounts, "transactionPricePerShare") if amounts is not None else None,
        acquired_disposed=(
            _text(amounts, "transactionAcquiredDisposedCode") if amounts is not None else None
        ),
        shares_owned_after=(
            _decimal(post, "sharesOwnedFollowingTransaction") if post is not None else None
        ),
        direct_or_indirect=(
            _text(nature, "directOrIndirectOwnership") if nature is not None else None
        ),
        is_derivative=is_derivative,
    )


def parse_form4(xml_bytes: bytes) -> Form4:
    """Parse a Form 4 ownership XML. Always returns a Form4, never raises."""
    doc = Form4()

    try:
        # recover=True gets us through unescaped ampersands and stray bytes,
        # which really do show up in filings from smaller filing agents.
        parser = etree.XMLParser(recover=True, resolve_entities=False, no_network=True)
        root = etree.fromstring(xml_bytes, parser=parser)
    except Exception as exc:                      # noqa: BLE001 - want everything
        doc.warnings.append(f"xml-unparseable: {exc}")
        return doc

    if root is None:
        doc.warnings.append("xml-empty")
        return doc

    doc.document_type = (root.findtext("documentType") or "").strip() or None
    doc.period_of_report = (root.findtext("periodOfReport") or "").strip() or None

    issuer = root.find("issuer")
    if issuer is None:
        doc.warnings.append("missing-issuer")
    else:
        doc.issuer_cik = (issuer.findtext("issuerCik") or "").strip().lstrip("0") or None
        doc.issuer_name = (issuer.findtext("issuerName") or "").strip() or None
        sym = (issuer.findtext("issuerTradingSymbol") or "").strip()
        # Some filings put "NONE" / "N/A" here.
        doc.ticker = sym.upper() if sym and sym.upper() not in {"NONE", "N/A", "-"} else None

    for ro in root.findall("reportingOwner"):
        ident = ro.find("reportingOwnerId")
        rel = ro.find("reportingOwnerRelationship")
        doc.owners.append(
            Owner(
                cik=(ident.findtext("rptOwnerCik") or "").strip().lstrip("0") or None
                if ident is not None
                else None,
                name=(ident.findtext("rptOwnerName") or "").strip() or None
                if ident is not None
                else None,
                is_director=_flag(rel, "isDirector") if rel is not None else False,
                is_officer=_flag(rel, "isOfficer") if rel is not None else False,
                is_ten_percent_owner=_flag(rel, "isTenPercentOwner") if rel is not None else False,
                is_other=_flag(rel, "isOther") if rel is not None else False,
                officer_title=(rel.findtext("officerTitle") or "").strip() or None
                if rel is not None
                else None,
            )
        )
    if not doc.owners:
        doc.warnings.append("missing-reporting-owner")

    for tag, is_deriv in (
        ("nonDerivativeTable/nonDerivativeTransaction", False),
        ("derivativeTable/derivativeTransaction", True),
    ):
        for node in root.findall(tag):
            try:
                doc.transactions.append(_parse_transaction(node, is_deriv))
            except Exception as exc:              # noqa: BLE001
                doc.warnings.append(f"bad-transaction: {exc}")

    for fn in root.findall("footnotes/footnote"):
        # itertext() so nested markup inside a footnote still yields its words.
        txt = " ".join(t.strip() for t in fn.itertext() if t and t.strip())
        if txt:
            doc.footnotes.append(txt)

    if not doc.transactions:
        doc.warnings.append("no-transactions")

    return doc
