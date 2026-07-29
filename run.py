#!/usr/bin/env python3
"""
Insider purchase monitor -- entry point.

Modes
-----
  python run.py                      one poll of the live EDGAR feed (what CI runs)
  python run.py --dry-run            same, but print alerts instead of sending
  python run.py --self-test          parse the local fixtures only, no network
  python run.py --reconcile 2026-05-22
                                     re-read a whole day from the daily index
  python run.py --backfill 2026-05-15 2026-06-01
                                     walk a date range through the same pipeline

Nothing here places a trade, connects to a broker, or acts on a signal. It
reads public filings and sends you a message.
"""

from __future__ import annotations

import argparse
import logging
import sys
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from insider.config import load_config, secret, setup_logging
from insider.edgar import EdgarClient, FeedEntry
from insider.form4 import Owner, parse_form4, primary_owner
from insider.market import build_provider
from insider.notify import TelegramNotifier, format_signal
from insider.screen import screen
from insider.state import State

log = logging.getLogger("run")

# A filing that will not download after this many runs is almost certainly
# broken rather than briefly unavailable; stop retrying so it cannot clog
# every future run.
MAX_FETCH_TRIES = 3

# Reports are read against the US market day, so every timestamp shown to a
# human is New York time. %Z prints EDT or EST, so the label is never wrong
# across a daylight-saving switch.
NY = ZoneInfo("America/New_York")


def ny(dt: datetime) -> str:
    return dt.astimezone(NY).strftime("%Y-%m-%d %H:%M %Z")


def _process(
    entries: list[FeedEntry],
    client: EdgarClient,
    provider,
    notifier: TelegramNotifier,
    state: State,
    cfg: dict,
    limit: int,
) -> tuple[int, int]:
    """Run filings through the pipeline. Returns (examined, alerted)."""
    examined = alerted = 0

    for entry in entries:
        if examined >= limit:
            log.warning("hit max_filings_per_run (%s); rest waits for next run", limit)
            break
        if state.is_seen(entry.accession) and not entry.is_amendment:
            continue
        if state.already_alerted(entry.accession):
            continue

        examined += 1

        # Fetch BEFORE marking it seen. Marking first meant a single transient
        # network error buried the filing permanently -- 337 of them had piled
        # up that way.
        xml = client.fetch_form4_xml(entry.cik, entry.accession, entry.archive_path)
        if xml is None:
            tries = state.note_fetch_failure(entry.accession)
            state.bump("fetch_failed")
            if tries >= MAX_FETCH_TRIES:
                log.warning("giving up on %s after %s attempts", entry.accession, tries)
                state.mark_seen(entry.accession)
            else:
                log.info("fetch failed for %s (attempt %s), will retry", entry.accession, tries)
            continue

        state.clear_fetch_failure(entry.accession)
        state.mark_seen(entry.accession)

        doc = parse_form4(xml)
        if doc.warnings:
            log.debug("%s parse notes: %s", entry.accession, doc.warnings)

        # Cheapest possible early exit: the vast majority of Form 4s are
        # grants, sales and option exercises, so bail before touching any
        # market-data API.
        agg = doc.aggregate_purchase()
        if agg is None:
            state.bump("not_a_purchase")
            continue

        state.bump("purchases_found")

        # Filings often leave the trading symbol blank. Ask SEC directly before
        # giving up -- and if SEC has no ticker either, the issuer is not
        # publicly traded (non-traded BDCs and private REITs file Form 4s all
        # the time). There is no market to act on, so it is noise, not a signal.
        if not doc.ticker and doc.issuer_cik:
            sym, exch = client.ticker_for_cik(doc.issuer_cik)
            if sym:
                doc.ticker = sym
                log.info("resolved ticker %s for CIK %s", sym, doc.issuer_cik)
        if not doc.ticker and cfg["filters"].get("require_ticker", True):
            log.info(
                "skip %s: %s has no ticker (not publicly traded)",
                entry.accession, doc.issuer_name or doc.issuer_cik,
            )
            state.bump("no_ticker")
            continue

        snap = provider.snapshot(doc.ticker) if doc.ticker else None
        # The daily index carries a date but no clock time. Rendering midnight
        # UTC as New York time would show 20:00 on the PREVIOUS day, so say
        # plainly that only the date is known.
        filed = ny(entry.filed_at) if entry.filed_at_is_exact else f"{entry.filed_at:%Y-%m-%d}"

        # ONE alert per filing. Reporting owners are co-filers on the SAME
        # transaction, not separate buyers -- a single KLRS purchase listed
        # five owners and produced five identical Telegram messages before
        # this was fixed.
        owners = doc.owners or [Owner(cik=None, name=None)]
        sent_for_this_filing = False

        sig, rej = screen(
            doc,
            primary_owner(owners),
            accession=entry.accession,
            filing_url=entry.filing_url,
            filed_at=filed,
            market=snap,
            cfg=cfg["filters"],
        )
        if sig is None:
            log.info("skip %s: %s", entry.accession, rej.reason)
            state.bump("filtered_out")
            continue

        if len(owners) > 1:
            others = ", ".join(o.name for o in owners if o.name and o.name != sig.owner_name)
            sig.notes.append(
                f"Filing'da {len(owners)} ta reporting owner, bitta xarid"
                + (f" (yana: {others})" if others else "")
            )

        # Same trade, different document: co-filers and 4/A restatements.
        if state.already_alerted_fp(sig.fingerprint):
            log.info(
                "skip %s: this purchase was already alerted under another filing",
                entry.accession,
            )
            state.bump("duplicate_purchase")
            if not notifier.dry_run:
                state.mark_alerted(entry.accession)
            continue

        if notifier.send(format_signal(sig)):
            alerted += 1
            sent_for_this_filing = True
            log.info("ALERT %s %s $%s", sig.ticker, sig.owner_name, f"{sig.value_usd:,.0f}")
        else:
            log.error("failed to deliver alert for %s", entry.accession)
            state.bump("send_failed")

        # A dry run must never consume the alert. Marking it here would make
        # the first real run skip the filing as "already sent" and the signal
        # would be lost silently.
        if sent_for_this_filing and not notifier.dry_run:
            state.mark_alerted(entry.accession)
            state.mark_alerted_fp(sig.fingerprint)

    return examined, alerted


def _self_test(notifier: TelegramNotifier, cfg: dict) -> int:
    """Offline end-to-end check against the bundled fixtures."""
    from insider.form4 import Owner
    from insider.market import MarketSnapshot
    from decimal import Decimal

    fixtures = sorted((Path(__file__).parent / "tests" / "fixtures").glob("*.xml"))
    if not fixtures:
        log.error("no fixtures found")
        return 1

    print(f"\n=== SELF-TEST: {len(fixtures)} fixtures, no network ===\n")
    passed = 0
    for f in fixtures:
        doc = parse_form4(f.read_bytes())
        agg = doc.aggregate_purchase()
        verdict = "PURCHASE" if agg else "not a purchase"
        extra = ""
        if agg:
            extra = f" -> {agg['shares']:,.0f} x ${agg['price']} = ${agg['value_usd']:,.0f}"
            if doc.is_subscription:
                extra += "  [SUBSCRIPTION]"
        print(f"  {f.name:28s} {verdict}{extra}")
        passed += 1

        if agg and doc.owners:
            # Pretend cap so the 1% rule is exercised on the VCIG fixture.
            snap = MarketSnapshot(
                ticker=doc.ticker or "?",
                price=Decimal("0.5149"),
                market_cap=Decimal("6900000"),
                shares_outstanding=Decimal("13400000"),
                exchange="NasdaqCM",
                country="Malaysia",
                week52_low=Decimal("0.36"),
                week52_high=Decimal("4.30"),
                source="self-test",
            )
            sig, rej = screen(
                doc,
                doc.owners[0],
                accession="0000000000-00-000000",
                filing_url="https://www.sec.gov/",
                filed_at="2026-05-22 08:01 EDT",
                market=snap,
                cfg=cfg["filters"],
            )
            if sig:
                notifier.send(format_signal(sig))
            else:
                print(f"      screened out: {rej.reason}")

    print(f"\n=== {passed} fixtures parsed, no crashes ===\n")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="SEC Form 4 insider purchase monitor")
    ap.add_argument("--dry-run", action="store_true", help="print alerts, send nothing")
    ap.add_argument("--self-test", action="store_true", help="offline fixture run")
    ap.add_argument(
        "--test-telegram",
        action="store_true",
        help="send one message to prove the chat/topic works, then exit",
    )
    ap.add_argument("--reconcile", metavar="YYYY-MM-DD", help="re-read one day fully")
    ap.add_argument("--backfill", nargs=2, metavar=("START", "END"))
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    load_dotenv()
    setup_logging(args.verbose)
    cfg = load_config(args.config)

    notifier = TelegramNotifier(
        secret("TELEGRAM_BOT_TOKEN"),
        secret("TELEGRAM_CHAT_ID"),
        thread_id=secret("TELEGRAM_THREAD_ID"),
        dry_run=args.dry_run,
    )

    if args.test_telegram:
        target = f"chat {notifier.chat_id}"
        if notifier.thread_id:
            target += f", topic {notifier.thread_id}"
        ok = notifier.send(
            "\u2705 <b>insider_alert ulanish testi</b>\n"
            f"Vaqt: {ny(datetime.now(timezone.utc))}\n"
            f"Manzil: {target}"
        )
        log.info("test message %s", "delivered" if ok else "FAILED")
        return 0 if ok else 1

    if args.self_test:
        return _self_test(notifier, cfg)

    ua = secret("SEC_USER_AGENT")
    if not ua:
        log.error("SEC_USER_AGENT is not set -- SEC will return 403. See .env.example")
        return 2

    state = State.load(cfg["run"]["state_file"])
    client = EdgarClient(ua, per_second=cfg["edgar"]["requests_per_second"])
    provider = build_provider(cfg["market"]["provider"])
    limit = int(cfg["run"]["max_filings_per_run"])

    try:
        if args.backfill:
            start = date.fromisoformat(args.backfill[0])
            end = date.fromisoformat(args.backfill[1])
            entries: list[FeedEntry] = []
            day = start
            while day <= end:
                if day.weekday() < 5:  # EDGAR does not accept filings at weekends
                    got = client.daily_index_form4s(datetime.combine(day, datetime.min.time()))
                    log.info("%s: %s Form 4s in daily index", day, len(got))
                    entries.extend(got)
                day += timedelta(days=1)
            limit = max(limit, len(entries))
        elif args.reconcile:
            day = date.fromisoformat(args.reconcile)
            entries = client.daily_index_form4s(datetime.combine(day, datetime.min.time()))
            log.info("%s: %s Form 4s in daily index", day, len(entries))
            limit = max(limit, len(entries))
        else:
            entries = client.recent_form4s(
                max_pages=cfg["edgar"]["feed_pages"],
                page_size=cfg["edgar"]["feed_page_size"],
            )
            log.info("feed returned %s unique Form 4 filings", len(entries))

        examined, alerted = _process(
            entries, client, provider, notifier, state, cfg, limit
        )
        state.bump("runs")
        state.finish_run()
        if args.dry_run:
            log.info("dry-run: state deliberately NOT saved")
        else:
            state.save()
        log.info(
            "done: %s examined, %s alerts, counters=%s", examined, alerted, state.counters
        )
        return 0

    except Exception:  # noqa: BLE001
        # GitHub sends no notification when a scheduled workflow fails, so the
        # failure has to page us through the same channel as the signals.
        tb = traceback.format_exc(limit=4)
        log.error("run failed:\n%s", tb)
        if cfg["run"].get("alert_on_failure") and not args.dry_run:
            try:
                notifier.send(
                    "🔴 <b>insider_alert xatolik bilan to'xtadi</b>\n"
                    f"<pre>{tb[-1200:]}</pre>"
                )
            except Exception:  # noqa: BLE001
                pass
        if not args.dry_run:
            try:
                state.save()
            except Exception:  # noqa: BLE001
                pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
