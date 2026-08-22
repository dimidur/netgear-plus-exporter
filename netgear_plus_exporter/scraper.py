"""One switch's session, and how often it is actually polled.

Changes when the retry, caching or concurrency policy changes. It knows what
a poll costs and what to serve when one cannot be taken; it does not know how
the switch answers, nor what a Prometheus metric is.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from . import upstream
from .errors import SwitchScrapeError

_LOGGER = logging.getLogger(__name__)

# What to say about each reason upstream can refuse the counters. The first
# "%s" is the host, filled in by _warn_degraded; ROW_COUNT's remaining three
# come from Degraded.detail, in that order.
_DEGRADED_TEXT = {
    upstream.INCOMPLETE_POLL: (
        "%s did not complete a full poll, so the retained statistics are from "
        "an earlier one; not trusting the raw path"
    ),
    upstream.NO_RETAINED_DATA: (
        "no retained statistics for %s; falling back to the rounded counters, "
        "or failing the scrape if the raw path was already in use"
    ),
    upstream.MISSING_LISTS: (
        "retained statistics for %s are missing the counter lists; not trusting the raw path"
    ),
    upstream.ROW_COUNT: (
        "statistics row count disagrees with the port count for %s: %s has %d "
        "rows for %d ports; not trusting the raw path"
    ),
}


class NetgearSwitchScraper:
    """Owns the connection to one switch and caches its last good reading."""

    def __init__(
        self,
        host: str,
        password: str,
        cache_seconds: float = 30.0,
    ) -> None:
        self.host = host
        self.cache_seconds = cache_seconds
        self._password = password
        self._connector: upstream.Connector | None = None
        self._lock = threading.Lock()
        # What the last attempt produced: a reading, the exception it raised,
        # or None before the first one.
        self._outcome: dict[str, Any] | Exception | None = None
        # Counters from the last SUCCESSFUL poll, which outlive a run of
        # failures because the zero-transition guard compares against them.
        self._history: dict[str, list[int]] | None = None
        # -inf rather than 0.0: time.monotonic() is uptime on Linux, so a
        # process started seconds after boot would read 0.0 as fresh.
        self._attempted_at = float("-inf")
        self._warned: set[str] = set()

    def scrape(self) -> dict[str, Any]:
        """Return a reading, replaying the last outcome while it is fresh.

        Failures are cached like successes, and a caller that cannot take the
        lock replays instead of queueing. A poll against an unreachable switch
        runs to about 90 seconds, longer than a Prometheus scrape timeout, so
        without both the endpoint stops answering rather than answering
        netgear_plus_up 0.
        """
        if not self._lock.acquire(blocking=False):
            return self._replay()
        try:
            if time.monotonic() - self._attempted_at < self.cache_seconds:
                return self._replay()
            return self._poll()
        finally:
            self._lock.release()

    def _connect(self) -> upstream.Connector:
        return upstream.connect(self.host, self._password)

    def _warn_degraded(self, degraded: upstream.Degraded) -> None:
        """Warn once per reason, then drop to debug. The latch never resets.

        A placeholder mismatch between the text and ``detail`` is a stderr
        notice rather than an exception, so it loses the message silently.
        """
        level = logging.DEBUG if degraded.reason in self._warned else logging.WARNING
        self._warned.add(degraded.reason)
        _LOGGER.log(level, _DEGRADED_TEXT[degraded.reason], self.host, *degraded.detail)

    def _replay(self) -> dict[str, Any]:
        """The last attempt's outcome, without touching the switch.

        Called with the lock held and, from scrape()'s non-blocking path,
        without it, while the lock's owner may be reassigning ``_outcome``.
        Snapshotted once for that reason: read repeatedly, it could be an
        exception at the check and a reading by the return.
        """
        outcome = self._outcome
        if isinstance(outcome, Exception):
            # A new exception rather than a re-raise: raising one object
            # repeatedly appends a frame to its traceback every time, which
            # grows without bound for as long as the switch stays down.
            msg = f"{self.host} failed its last poll, still within the cache interval"
            raise SwitchScrapeError(msg) from outcome
        if outcome is None:
            msg = f"no reading for {self.host} yet"
            raise SwitchScrapeError(msg)
        return outcome

    def _poll(self) -> dict[str, Any]:
        """Poll the switch, recording the outcome for _replay to serve."""
        try:
            reading = self._read()
        except Exception as exc:  # noqa: BLE001 - recorded, then re-raised unchanged
            self._outcome = exc
            raise
        else:
            self._outcome = reading
            if reading["raw"] is not None:
                # A poll can succeed with no counters at all, and dropping the
                # history then leaves the zero-transition guard with nothing to
                # compare against: the next zero-padded page reads as a genuine
                # counter reset. Stale history is the safe side of that.
                self._history = reading["raw"]
            return reading
        finally:
            self._attempted_at = time.monotonic()

    def _read(self) -> dict[str, Any]:
        """One poll, with one reconnect retry. The caller holds the lock."""
        started = time.monotonic()
        if self._connector is None:
            self._connector = self._connect()
        polled_before = upstream.poll_timestamp(self._connector)
        try:
            infos = self._connector.get_switch_infos()
        except Exception as poll_exc:  # noqa: BLE001 - several unrelated types mean "poll failed"
            # Rebuild the session once before giving up; the library refreshes
            # an expired cookie in place, so this is some other failure. Logged
            # only after a successful retry, where nothing else records it: a
            # failed retry carries it as __context__ of the wrapper below.
            try:
                self._connector = self._connect()
                # The replacement connector carries its own timestamp.
                polled_before = upstream.poll_timestamp(self._connector)
                infos = self._connector.get_switch_infos()
            except Exception as retry_exc:
                msg = f"the poll of {self.host} failed again after reconnecting: {retry_exc!r}"
                raise SwitchScrapeError(msg) from retry_exc
            # Latched like the degraded reasons: a switch that fails one poll
            # per cycle and recovers on the retry is a real fault, but one
            # traceback per cache interval buries everything else in the log.
            recurring = type(poll_exc).__qualname__ in self._warned
            self._warned.add(type(poll_exc).__qualname__)
            _LOGGER.warning(
                "poll of %s failed; reconnected and retried",
                self.host,
                exc_info=None if recurring else poll_exc,
            )

        if not infos:
            msg = f"empty response from {self.host}"
            raise SwitchScrapeError(msg)

        raw, degraded = upstream.read_statistics(
            self._connector, polled_before, self._history, self.host
        )
        if degraded is not None:
            self._warn_degraded(degraded)

        return {
            "infos": infos,
            "raw": raw,
            "ports": upstream.port_count(self._connector),
            "model": upstream.model_name(self._connector),
            "duration": time.monotonic() - started,
        }
