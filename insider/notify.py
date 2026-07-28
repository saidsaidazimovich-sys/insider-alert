"""Telegram delivery: HTML formatting, throttling, dry-run."""

from __future__ import annotations

import html
import logging
import time
from decimal import Decimal

import requests

log = logging.getLogger(__name__)

API = "https://api.telegram.org"
# Telegram allows roughly 20 messages/minute to one chat.
MIN_GAP_SECONDS = 3.2


def _money(v: Decimal | None, dp: int = 0) -> str:
    if v is None:
        return "?"
    return f"${v:,.{dp}f}"


def _compact(v: Decimal | None) -> str:
    if v is None:
        return "?"
    v = Decimal(v)
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(v) >= Decimal(str(div)):
            return f"${v / Decimal(str(div)):,.1f}{unit}"
    return f"${v:,.0f}"


def format_signal(sig) -> str:
    e = html.escape
    m = sig.market

    head = "🟢 <b>INSAYDER XARIDI</b>"
    if sig.is_amendment:
        head += " (tuzatilgan)"

    meta = " | ".join(
        p
        for p in [
            f"Narx: {_money(m.price, 4) if m and m.price else '?'}",
            f"Kap: {_compact(m.market_cap) if m else '?'}",
            (m.exchange if m and m.exchange else None),
            (m.country if m and m.country else None),
        ]
        if p
    )

    lines = [
        head,
        f"📊 <b>{e(sig.ticker or '?')}</b> · {e(sig.issuer_name or '?')}",
        f"   {e(meta)}",
        f"👤 {e(sig.owner_name or '?')} — {e(sig.role_label)}",
        f"   💰 {sig.shares:,.0f} × {_money(sig.price, 4)} = <b>{_money(sig.value_usd)}</b>",
    ]

    when = e(sig.transaction_date or "?")
    if sig.transaction_date_last and sig.transaction_date_last != sig.transaction_date:
        when += f" … {e(sig.transaction_date_last)}"
    lines.append(f"   📅 Operatsiya: {when} | Filing: {e(sig.filed_at)}")

    if sig.shares_owned_after is not None:
        lines.append(f"   📈 Xariddan keyin: {sig.shares_owned_after:,.0f} aksiya")

    if sig.pct_of_market_cap is not None:
        lines.append(f"🔍 Kapitallashuvning <b>{sig.pct_of_market_cap:.2f}%</b>i")

    if m and m.pct_from_52w_low is not None and m.pct_from_52w_high is not None:
        lines.append(
            f"   52 hafta: min'dan {m.pct_from_52w_low:+.0f}%, "
            f"max'dan {m.pct_from_52w_high:+.0f}%"
        )

    if sig.is_10b5_1:
        lines.append(
            "⚠️ <b>TURI: Rule 10b5-1 rejasi</b> — oldindan tuzilgan avtomatik "
            "sotib olish dasturi. Insayder o'sha kuni qaror qilmagan, shuning "
            "uchun bu kuchsizroq signal."
        )

    if sig.is_subscription:
        lines.append(
            "⚠️ <b>TURI: Kompaniyadan yozilish (subscription)</b> — ochiq bozor "
            "xaridi emas. Pul kompaniyaga kiradi va yangi aksiya chiqariladi "
            "(dilyutsiya)."
        )
        if sig.subscription_evidence:
            lines.append(f"   <i>Asos: “{e(sig.subscription_evidence)}”</i>")

    for note in sig.notes:
        lines.append(f"   • {e(note)}")

    lines.append(f'📄 <a href="{e(sig.filing_url)}">SEC Form 4</a>')
    return "\n".join(lines)


class TelegramNotifier:
    """Sends to a private chat, a group, or one topic inside a forum group.

    For a forum group you need two ids: the group's own chat_id (a negative
    number like -1001234567890) and the topic's message_thread_id. Without the
    thread id the message lands in the group's General topic instead.
    """

    def __init__(
        self,
        token: str | None,
        chat_id: str | None,
        thread_id: str | None = None,
        dry_run: bool = False,
    ):
        self.token = token
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.dry_run = dry_run
        self._last_send = 0.0
        if not dry_run and not (token and chat_id):
            raise ValueError(
                "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set "
                "(or run with --dry-run)"
            )

    def send(self, text: str) -> bool:
        if self.dry_run:
            print("\n" + "-" * 62)
            print(_strip_html(text))
            print("-" * 62)
            return True

        gap = MIN_GAP_SECONDS - (time.monotonic() - self._last_send)
        if gap > 0:
            time.sleep(gap)

        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if self.thread_id:
            payload["message_thread_id"] = int(self.thread_id)

        for attempt in range(3):
            try:
                r = requests.post(
                    f"{API}/bot{self.token}/sendMessage", json=payload, timeout=20
                )
            except requests.RequestException as exc:
                log.warning("telegram send failed: %s", exc)
            else:
                self._last_send = time.monotonic()
                if r.status_code == 200:
                    return True
                if r.status_code == 429:
                    wait = int(r.json().get("parameters", {}).get("retry_after", 5))
                    log.warning("telegram rate limited, waiting %ss", wait)
                    time.sleep(wait + 1)
                    continue
                # The two failures worth naming, because the fix is different:
                # a missing/renamed topic vs the bot not being in the group.
                body = r.text[:300]
                if "thread not found" in body.lower():
                    log.error(
                        "TELEGRAM_THREAD_ID %s does not exist in chat %s -- "
                        "the topic may have been deleted or the id is wrong",
                        self.thread_id, self.chat_id,
                    )
                elif "chat not found" in body.lower() or "bot is not a member" in body.lower():
                    log.error(
                        "bot cannot post to chat %s -- add it to the group and "
                        "allow it to send messages",
                        self.chat_id,
                    )
                else:
                    log.error("telegram HTTP %s: %s", r.status_code, body)
                return False
            time.sleep(2 ** attempt)
        return False


def _strip_html(text: str) -> str:
    import re

    return html.unescape(re.sub(r"<[^>]+>", "", text))
