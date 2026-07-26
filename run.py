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

from dotenv import load_dotenv

from insider.config import load_config, secret, setup_logging
from insider.edgar import EdgarClient, FeedEntry
from insider.form4 import Owner, parse_form4, primary_owner
from insider.market import build_provider
from insider.notify import TelegramNotifier, format_signal
from insider.screen import screen
from insider.state import State

log = logging.getLogger("run")


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
        state.mark_seen(entry.accession)

        xml = client.fetch_form4_xml(entry.cik, entry.accession)
        if xml is None:
            log.warning("could not fetch XML for %s", entry.accession)
            state.bump("fetch_failed")
            continue

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

        snap = provider.snapshot(doc.ticker) if doc.ticker else None
        filed = entry.filed_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

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
                filed_at="2026-05-22 12:01 UTC",
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
    ap.add_argument("--reconcile", metavar="YYYY-MM-DD", help="re-read one day fully")
    ap.add_argument("--backfill", nargs=2, metavar=("START", "END"))
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    load_dotenv()
    setup_logging(args.verbose)
    cfg = load_config(args.config)

    notifier = TelegramNotifier(
        secret("TELEGRAM_BOT_TOKEN"), secret("TELEGRAM_CHAT_ID"), dry_run=args.dry_run
    )

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
