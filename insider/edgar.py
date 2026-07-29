"""
EDGAR access layer.

SEC rules we must respect (breaking them gets the IP blocked, not just throttled):
  * every request carries a descriptive User-Agent with a contact address
  * Accept-Encoding: gzip, deflate
  * no more than 10 requests/second

Fetching strategy
-----------------
For each filing we pull the *complete submission text file*, whose name is
predictable: {accession-with-dashes}.txt. It contains the ownership XML inline
in a single <XML> block. Verified against real filings including one whose XML
document was named `wk-form4_1778291166.xml` rather than `ownership.xml` --
which is exactly why guessing the XML filename does not work. Using the .txt
means ONE request per filing instead of index.json + the XML.
index.json remains as a fallback for odd submissions.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

FEED_URL = "https://www.sec.gov/cgi-bin/browse-edgar"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data"
DAILY_INDEX = "https://www.sec.gov/Archives/edgar/daily-index"

_ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}


class RateLimiter:
    """Token bucket. Thread-safe, blocks until a slot is free."""

    def __init__(self, per_second: float = 8.0):
        self._min_gap = 1.0 / per_second
        self._lock = threading.Lock()
        self._last = 0.0

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._min_gap - (now - self._last)
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._last = now


@dataclass
class FeedEntry:
    """One row of the getcurrent Atom feed.

    The same filing appears once per party -- once as (Issuer) and once for each
    (Reporting) owner -- all sharing the accession number. We dedupe on that.
    """

    accession: str          # dashed form, e.g. 0001493152-26-022075
    cik: str                # whichever CIK this row was listed under
    form_type: str          # "4" or "4/A"
    filed_at: datetime
    title: str
    # Path straight out of the daily index, e.g.
    # "edgar/data/938543/0000950142-26-002183.txt". Authoritative, so we
    # prefer it over rebuilding a URL from a parsed CIK column.
    archive_path: str | None = None
    # The Atom feed gives a real accepted-at timestamp. The daily index gives
    # only a date, so anything derived from it must not be shown as a clock
    # time -- midnight UTC renders as 20:00 the PREVIOUS day in New York.
    filed_at_is_exact: bool = True

    @property
    def accession_nodash(self) -> str:
        return self.accession.replace("-", "")

    @property
    def is_amendment(self) -> bool:
        return self.form_type.strip().endswith("/A")

    @property
    def filing_url(self) -> str:
        return f"{ARCHIVE}/{self.cik}/{self.accession_nodash}/{self.accession}-index.htm"


class EdgarClient:
    def __init__(self, user_agent: str, per_second: float = 8.0, timeout: float = 20.0):
        if not user_agent or "@" not in user_agent:
            raise ValueError(
                "SEC requires a User-Agent like 'Name Surname you@example.com'. "
                "Set SEC_USER_AGENT in the environment."
            )
        self.timeout = timeout
        self.limiter = RateLimiter(per_second)
        self._ticker_cache: dict[str, tuple[str | None, str | None]] = {}
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept-Encoding": "gzip, deflate",
            }
        )

    # --- low level ----------------------------------------------------------
    def get(self, url: str, params: dict | None = None, tries: int = 4) -> requests.Response | None:
        """GET with backoff. Returns None rather than raising, so one bad
        filing can never take down the whole run."""
        delay = 1.0
        for attempt in range(1, tries + 1):
            self.limiter.acquire()
            try:
                r = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                log.warning("request failed (%s/%s) %s: %s", attempt, tries, url, exc)
            else:
                if r.status_code == 200:
                    return r
                # 403 from SEC usually means the UA was rejected -- retrying
                # harder will not help, but a transient 403 does happen.
                if r.status_code in (403, 429, 500, 502, 503, 504):
                    log.warning(
                        "HTTP %s (%s/%s) %s", r.status_code, attempt, tries, url
                    )
                else:
                    log.warning("HTTP %s %s -- not retrying", r.status_code, url)
                    return None
            if attempt < tries:
                time.sleep(delay)
                delay *= 2
        return None

    # --- the near-real-time feed --------------------------------------------
    def recent_form4s(self, max_pages: int = 6, page_size: int = 400) -> list[FeedEntry]:
        """Newest Form 4 / 4/A filings, newest first, deduped by accession.

        Paginates because during the post-close rush EDGAR can accept more
        filings in 15 minutes than a single page holds.
        """
        from lxml import etree

        seen: set[str] = set()
        out: list[FeedEntry] = []

        for page in range(max_pages):
            params = {
                "action": "getcurrent",
                "type": "4",
                "dateb": "",
                "owner": "include",
                "count": page_size,
                "start": page * page_size,
                "output": "atom",
            }
            r = self.get(FEED_URL, params=params)
            if r is None:
                break
            try:
                root = etree.fromstring(
                    r.content, etree.XMLParser(recover=True, no_network=True)
                )
            except Exception as exc:  # noqa: BLE001
                log.error("feed unparseable: %s", exc)
                break
            if root is None:
                break

            entries = root.findall("a:entry", _ATOM_NS)
            if not entries:
                break

            new_on_page = 0
            for e in entries:
                parsed = _parse_entry(e)
                if parsed is None:
                    continue
                if parsed.accession in seen:
                    continue
                seen.add(parsed.accession)
                out.append(parsed)
                new_on_page += 1

            if len(entries) < page_size:
                break
            if new_on_page == 0:
                break

        out.sort(key=lambda e: e.filed_at, reverse=True)
        return out

    # --- one filing ---------------------------------------------------------
    def fetch_form4_xml(
        self, cik: str, accession: str, archive_path: str | None = None
    ) -> bytes | None:
        """Return the raw ownership XML bytes for a filing, or None."""
        nodash = accession.replace("-", "")
        if archive_path:
            txt_url = f"https://www.sec.gov/Archives/{archive_path.lstrip('/')}"
        else:
            txt_url = f"{ARCHIVE}/{cik}/{nodash}/{accession}.txt"
        r = self.get(txt_url)
        if r is not None:
            xml = _extract_ownership_xml(r.content)
            if xml:
                return xml
            log.info("no ownership XML inside %s, falling back to index.json", txt_url)

        # Fallback: read the directory listing and pick the non-styled XML.
        r = self.get(f"{ARCHIVE}/{cik}/{nodash}/index.json")
        if r is None:
            return None
        try:
            items = r.json()["directory"]["item"]
        except (ValueError, KeyError, TypeError) as exc:
            log.warning("bad index.json for %s: %s", accession, exc)
            return None
        for item in items:
            name = str(item.get("name", ""))
            low = name.lower()
            # Skip the xsl-styled rendering; we want the raw data document.
            if low.endswith(".xml") and not low.startswith("xsl"):
                got = self.get(f"{ARCHIVE}/{cik}/{nodash}/{name}")
                if got is not None:
                    return got.content
        return None

    # --- issuer metadata ----------------------------------------------------
    def ticker_for_cik(self, cik: str) -> tuple[str | None, str | None]:
        """(ticker, exchange) for an issuer CIK, from SEC's submissions API.

        Filings often leave <issuerTradingSymbol> empty. Sometimes the company
        is simply not listed -- non-traded BDCs and private REITs file Form 4s
        constantly and there is no market to act on -- and sometimes the filer
        just omitted it. This tells the two apart instead of guessing.
        """
        key = str(cik).lstrip("0")
        if key in self._ticker_cache:
            return self._ticker_cache[key]

        result: tuple[str | None, str | None] = (None, None)
        r = self.get(f"https://data.sec.gov/submissions/CIK{int(key):010d}.json")
        if r is not None:
            try:
                j = r.json()
                tickers = j.get("tickers") or []
                exchanges = j.get("exchanges") or []
                if tickers:
                    result = (
                        str(tickers[0]).upper(),
                        str(exchanges[0]) if exchanges else None,
                    )
            except (ValueError, KeyError, TypeError, IndexError) as exc:
                log.warning("bad submissions JSON for CIK %s: %s", key, exc)
        self._ticker_cache[key] = result
        return result

    # --- gap filling --------------------------------------------------------
    def daily_index_form4s(self, day: datetime) -> list[FeedEntry]:
        """All Form 4s EDGAR accepted on `day`, from the daily index.

        The 15-minute feed poll can miss filings during heavy bursts, so a
        nightly pass over this index is what guarantees nothing is lost.
        """
        quarter = (day.month - 1) // 3 + 1
        url = f"{DAILY_INDEX}/{day.year}/QTR{quarter}/form.{day:%Y%m%d}.idx"
        r = self.get(url)
        if r is None:
            return []

        out: list[FeedEntry] = []
        for line in r.text.splitlines():
            # Fixed-width-ish, pipe-free layout: Form Type, Company, CIK, Date, File
            if not line.strip() or line.lstrip()[0] == "-":
                continue
            parts = re.split(r"\s{2,}", line.strip())
            if len(parts) < 5:
                continue
            form_type, company, date_str, path = (
                parts[0], parts[1], parts[3], parts[-1].strip()
            )
            if form_type.strip() not in ("4", "4/A"):
                continue
            # Company names can themselves contain runs of spaces, which shifts
            # every later column. The trailing path is unambiguous, so both the
            # CIK and the accession come from there.
            m = re.search(r"data/(\d+)/(\d{10}-\d{2}-\d{6})", path)
            if not m:
                continue
            try:
                filed = datetime.strptime(date_str.strip(), "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                filed = day
            out.append(
                FeedEntry(
                    accession=m.group(2),
                    cik=m.group(1).lstrip("0"),
                    form_type=form_type.strip(),
                    filed_at=filed,
                    title=company.strip(),
                    archive_path=path,
                    filed_at_is_exact=False,
                )
            )
        # One accession can appear per party here too.
        uniq: dict[str, FeedEntry] = {}
        for e in out:
            uniq.setdefault(e.accession, e)
        return list(uniq.values())


# --- helpers ---------------------------------------------------------------
_XML_BLOCK = re.compile(rb"<XML>\s*(.*?)\s*</XML>", re.DOTALL | re.IGNORECASE)


def _extract_ownership_xml(submission: bytes) -> bytes | None:
    """Pull the ownershipDocument out of a complete submission text file."""
    for m in _XML_BLOCK.finditer(submission):
        block = m.group(1).strip()
        if b"<ownershipDocument" in block:
            return block
    return None


def _parse_entry(entry) -> FeedEntry | None:
    title = (entry.findtext("a:title", default="", namespaces=_ATOM_NS) or "").strip()
    summary = (entry.findtext("a:summary", default="", namespaces=_ATOM_NS) or "")
    updated = (entry.findtext("a:updated", default="", namespaces=_ATOM_NS) or "").strip()

    # <id>urn:tag:sec.gov,2008:accession-number=0001493152-26-022075</id>
    ident = entry.findtext("a:id", default="", namespaces=_ATOM_NS) or ""
    m = re.search(r"(\d{10}-\d{2}-\d{6})", ident) or re.search(
        r"AccNo:</b>\s*(\d{10}-\d{2}-\d{6})", summary
    )
    if not m:
        return None
    accession = m.group(1)

    # CIK comes from the archive path in the alternate link.
    cik = ""
    link = entry.find("a:link", _ATOM_NS)
    if link is not None:
        lm = re.search(r"/data/(\d+)/", link.get("href", "") or "")
        if lm:
            cik = lm.group(1)
    if not cik:
        cm = re.search(r"\((\d{10})\)", title)
        cik = cm.group(1).lstrip("0") if cm else ""

    form_type = "4/A" if title.startswith("4/A") else "4"

    try:
        filed_at = datetime.fromisoformat(updated)
    except ValueError:
        filed_at = datetime.now(timezone.utc)
    if filed_at.tzinfo is None:
        filed_at = filed_at.replace(tzinfo=timezone.utc)

    return FeedEntry(
        accession=accession,
        cik=cik,
        form_type=form_type,
        filed_at=filed_at,
        title=title,
    )
