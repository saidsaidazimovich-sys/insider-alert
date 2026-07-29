"""Config loading: config.yaml for behaviour, environment for secrets."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

DEFAULTS = {
    "filters": {
        "min_value_usd": 250_000,
        "min_pct_of_market_cap": 1.0,
        "allowed_exchanges": [],
        "exclude_tickers": [],
        "require_ticker": True,
        "alert_when_cap_unknown": True,
    },
    "market": {"provider": "yfinance"},
    "edgar": {"requests_per_second": 8, "feed_pages": 6, "feed_page_size": 400},
    "run": {
        "state_file": "state/state.json",
        "max_filings_per_run": 400,
        "alert_on_failure": True,
    },
}


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def load_config(path: str | Path = "config.yaml") -> dict:
    p = Path(path)
    user = {}
    if p.exists():
        try:
            user = yaml.safe_load(p.read_text()) or {}
        except yaml.YAMLError as exc:
            log.error("config.yaml is not valid YAML: %s", exc)
            sys.exit(2)
    else:
        log.warning("%s not found, using defaults", p)
    return _merge(DEFAULTS, user)


def setup_logging(verbose: bool = False) -> None:
    """Plain text to stderr -- GitHub Actions logs read better without JSON."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-18s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("yfinance").setLevel(logging.ERROR)


def secret(name: str) -> str | None:
    v = os.getenv(name)
    return v.strip() if v and v.strip() else None
