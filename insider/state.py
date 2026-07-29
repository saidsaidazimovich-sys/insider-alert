"""
Run state, sized for GitHub Actions.

There is no persistent disk between workflow runs, so state is a small JSON
file committed back into the repo. That keeps it human-readable, diffable, and
-- usefully -- the commits themselves keep the repo active, which stops GitHub
from auto-disabling scheduled workflows after 60 days of inactivity.

We only need two things to be correct:
  * never alert twice on the same accession
  * know roughly where we got to, so a run does not re-examine the world
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# Plenty for weeks of filings; keeps the committed file small.
MAX_SEEN = 8000


@dataclass
class State:
    path: Path
    seen: list[str] = field(default_factory=list)          # accessions examined
    alerted: dict[str, str] = field(default_factory=dict)  # accession -> ISO sent_at
    # Fingerprint of the PURCHASE itself, so the same trade filed separately by
    # a fund, its GP and its manager -- or re-filed later as a 4/A -- alerts
    # once instead of once per document.
    alerted_fps: dict[str, str] = field(default_factory=dict)
    # accession -> how many times fetching it has failed, so a transient error
    # is retried on the next run rather than silently swallowed forever.
    fetch_failures: dict[str, int] = field(default_factory=dict)
    last_run: str | None = None
    counters: dict[str, int] = field(default_factory=dict)

    # --- persistence --------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path) -> "State":
        p = Path(path)
        if not p.exists():
            log.info("no state file at %s, starting fresh", p)
            return cls(path=p)
        try:
            raw = json.loads(p.read_text())
        except (OSError, ValueError) as exc:
            log.error("state file unreadable (%s); starting fresh", exc)
            return cls(path=p)
        return cls(
            path=p,
            seen=list(raw.get("seen", []))[-MAX_SEEN:],
            alerted=dict(raw.get("alerted", {})),
            alerted_fps=dict(raw.get("alerted_fps", {})),
            fetch_failures=dict(raw.get("fetch_failures", {})),
            last_run=raw.get("last_run"),
            counters=dict(raw.get("counters", {})),
        )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.seen = self.seen[-MAX_SEEN:]
        # Keep the alert log bounded too, newest first.
        if len(self.alerted) > MAX_SEEN:
            newest = sorted(self.alerted.items(), key=lambda kv: kv[1], reverse=True)
            self.alerted = dict(newest[:MAX_SEEN])
        if len(self.alerted_fps) > MAX_SEEN:
            newest = sorted(self.alerted_fps.items(), key=lambda kv: kv[1], reverse=True)
            self.alerted_fps = dict(newest[:MAX_SEEN])
        payload = {
            "last_run": self.last_run,
            "counters": self.counters,
            "alerted": self.alerted,
            "alerted_fps": self.alerted_fps,
            "fetch_failures": self.fetch_failures,
            "seen": self.seen,
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=1, sort_keys=True))
        tmp.replace(self.path)

    # --- queries ------------------------------------------------------------
    def is_seen(self, accession: str) -> bool:
        return accession in self._seen_set

    def already_alerted(self, accession: str) -> bool:
        return accession in self.alerted

    def mark_seen(self, accession: str) -> None:
        if accession not in self._seen_set:
            self.seen.append(accession)
            self._seen_set.add(accession)

    def mark_alerted(self, accession: str) -> None:
        self.alerted[accession] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # --- purchase-level dedup ------------------------------------------------
    def already_alerted_fp(self, fingerprint: str) -> bool:
        return fingerprint in self.alerted_fps

    def mark_alerted_fp(self, fingerprint: str) -> None:
        self.alerted_fps[fingerprint] = datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        )

    # --- fetch retries -------------------------------------------------------
    def note_fetch_failure(self, accession: str) -> int:
        n = self.fetch_failures.get(accession, 0) + 1
        self.fetch_failures[accession] = n
        return n

    def clear_fetch_failure(self, accession: str) -> None:
        self.fetch_failures.pop(accession, None)

    def bump(self, key: str, n: int = 1) -> None:
        self.counters[key] = self.counters.get(key, 0) + n

    def finish_run(self) -> None:
        self.last_run = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Lazily built lookup set; `seen` stays a list so the JSON diff is readable.
    @property
    def _seen_set(self) -> set[str]:
        cached = getattr(self, "_seen_cache", None)
        if cached is None or len(cached) != len(self.seen):
            cached = set(self.seen)
            object.__setattr__(self, "_seen_cache", cached)
        return cached
