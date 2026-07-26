"""
Market data enrichment.

The 1%-of-market-cap rule lives or dies on the market cap number, so this
module is deliberately careful about it:

  * marketCap straight from a data provider is frequently stale or missing for
    nano-caps -- exactly the companies this tool hunts. So we prefer
    sharesOutstanding x price, which is computed from two fields that are much
    more reliably present, and fall back to the provider's own marketCap.
  * When we genuinely cannot establish a cap we do NOT quietly drop the filing.
    A $2M CEO purchase that we skipped because Yahoo had no data is a far worse
    outcome than one alert with a "cap unknown" warning on it.
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal

log = logging.getLogger(__name__)


@dataclass
class MarketSnapshot:
    ticker: str
    price: Decimal | None = None
    market_cap: Decimal | None = None
    shares_outstanding: Decimal | None = None
    exchange: str | None = None
    sector: str | None = None
    country: str | None = None
    week52_low: Decimal | None = None
    week52_high: Decimal | None = None
    avg_volume: Decimal | None = None
    source: str = "none"
    error: str | None = None

    @property
    def cap_known(self) -> bool:
        return self.market_cap is not None and self.market_cap > 0

    @property
    def pct_from_52w_low(self) -> Decimal | None:
        if self.price and self.week52_low and self.week52_low > 0:
            return (self.price - self.week52_low) / self.week52_low * 100
        return None

    @property
    def pct_from_52w_high(self) -> Decimal | None:
        if self.price and self.week52_high and self.week52_high > 0:
            return (self.price - self.week52_high) / self.week52_high * 100
        return None


class MarketDataProvider(ABC):
    """Swap-in point. Add a provider, change one config line, nothing else."""

    name = "abstract"

    @abstractmethod
    def snapshot(self, ticker: str) -> MarketSnapshot: ...


def _dec(v) -> Decimal | None:
    if v is None:
        return None
    try:
        d = Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None
    return d if d.is_finite() else None


class YFinanceProvider(MarketDataProvider):
    name = "yfinance"

    def snapshot(self, ticker: str) -> MarketSnapshot:
        snap = MarketSnapshot(ticker=ticker, source=self.name)
        try:
            import yfinance as yf

            info = yf.Ticker(ticker).info or {}
        except Exception as exc:  # noqa: BLE001
            snap.error = f"{type(exc).__name__}: {exc}"
            log.warning("yfinance failed for %s: %s", ticker, snap.error)
            return snap

        snap.price = _dec(
            info.get("currentPrice")
            or info.get("regularMarketPrice")
            or info.get("previousClose")
        )
        snap.shares_outstanding = _dec(info.get("sharesOutstanding"))
        snap.exchange = info.get("fullExchangeName") or info.get("exchange")
        snap.sector = info.get("sector")
        snap.country = info.get("country")
        snap.week52_low = _dec(info.get("fiftyTwoWeekLow"))
        snap.week52_high = _dec(info.get("fiftyTwoWeekHigh"))
        snap.avg_volume = _dec(
            info.get("averageVolume3Month") or info.get("averageVolume")
        )

        # Prefer the computed cap -- see the module docstring.
        if snap.price and snap.shares_outstanding:
            snap.market_cap = snap.price * snap.shares_outstanding
        else:
            snap.market_cap = _dec(info.get("marketCap"))

        if not snap.cap_known:
            snap.error = snap.error or "market cap unavailable"
        return snap


class FinvizEliteProvider(MarketDataProvider):
    """Template. Said has a Finviz Elite subscription; the export endpoint is
    the sanctioned way to use it. Token comes from the environment, never code.

    Left unimplemented on purpose rather than half-guessed: the Elite export
    column layout needs to be read off a real authenticated response first.
    """

    name = "finviz-elite"

    def __init__(self):
        self.token = os.getenv("FINVIZ_AUTH_TOKEN")

    def snapshot(self, ticker: str) -> MarketSnapshot:
        if not self.token:
            return MarketSnapshot(
                ticker=ticker, source=self.name, error="FINVIZ_AUTH_TOKEN not set"
            )
        raise NotImplementedError(
            "FinvizEliteProvider is a stub -- verify the Elite export columns first."
        )


class CachedProvider(MarketDataProvider):
    """In-run memo. One GitHub Actions run is short-lived, so a process-local
    dict is the whole cache we need -- no TTL bookkeeping, no stale rows."""

    def __init__(self, inner: MarketDataProvider):
        self.inner = inner
        self.name = f"cached:{inner.name}"
        self._memo: dict[str, MarketSnapshot] = {}

    def snapshot(self, ticker: str) -> MarketSnapshot:
        key = ticker.upper()
        if key not in self._memo:
            self._memo[key] = self.inner.snapshot(key)
        return self._memo[key]


def build_provider(name: str) -> MarketDataProvider:
    providers = {"yfinance": YFinanceProvider, "finviz": FinvizEliteProvider}
    if name not in providers:
        raise ValueError(f"unknown market provider {name!r}; have {sorted(providers)}")
    return CachedProvider(providers[name]())
