"""Prometheus exporter for NETGEAR Plus / Easy Smart Managed switches.

These switches expose no SNMP. Everything here comes from scraping the web UI,
via :mod:`py_netgear_plus`, which handles the login handshake and the
per-model HTML quirks.

Two data paths are used deliberately:

* **Counters** (rx/tx bytes, CRC errors) are read from the library's *parser*
  layer. ``get_switch_infos()`` rounds the byte counters to megabytes, and
  reports CRC as a per-interval delta for one port; the parser returns the
  byte counters unrounded and CRC cumulative, for every port.
* **Everything else** (link state, negotiated speed, port description, firmware,
  serial) comes from ``get_switch_infos()``, where rounding is irrelevant.

The parser layer is not a stable public API, so it is probed defensively; a
fallback to the megabyte values scaled back to bytes exists, but whether it is
used is decided once per process -- see ``NetgearSwitchCollector._effective_raw``.
``netgear_plus_raw_counters`` reports which path produced the exported data.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Iterator
from typing import Any

import py_netgear_plus
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily, Metric
from prometheus_client.registry import Collector

_LOGGER = logging.getLogger(__name__)

BYTES_PER_MEGABYTE = 1_000_000  # py_netgear_plus uses decimal MB (1e-6)


def _bytes_went_all_zero(
    previous: dict[str, list[int]] | None, current: dict[str, list[int]]
) -> bool:
    """True when every rx/tx byte counter is zero but the previous reading
    had traffic somewhere.

    CRC counters are excluded on both sides: all-zero CRC is the healthy
    steady state, so it can neither establish "there was data before" nor
    count against the current reading.
    """
    if previous is None:
        return False
    had_traffic = any(any(previous.get(key) or []) for key in ("sum_rx", "sum_tx"))
    all_zero_now = not any(any(current.get(key) or []) for key in ("sum_rx", "sum_tx"))
    return had_traffic and all_zero_now


class SwitchScrapeError(Exception):
    """Raised when a scrape cycle fails."""


class NetgearSwitchScraper:
    """Owns the connection to one switch and caches its last good reading."""

    def __init__(
        self,
        host: str,
        password: str,
        name: str | None = None,
        cache_seconds: float = 30.0,
    ) -> None:
        self.host = host
        self.name = name or host
        self.cache_seconds = cache_seconds
        self._password = password
        self._connector: Any | None = None
        self._lock = threading.Lock()
        self._cached: dict[str, Any] | None = None
        self._cached_at = 0.0

    def _connect(self) -> Any:
        connector = py_netgear_plus.NetgearSwitchConnector(self.host, self._password)
        connector.autodetect_model()
        if not connector.get_login_cookie():
            msg = f"login rejected by {self.host}"
            raise SwitchScrapeError(msg)
        return connector

    def _raw_port_statistics(
        self, connector: Any, previous_raw: dict[str, list[int]] | None
    ) -> dict[str, list[int]] | None:
        """Return unrounded per-port counters, or None if unavailable.

        Reaches into the library's fetch/parse layer on purpose -- see module
        docstring. A structural change upstream lands here as None and the
        collector decides what that means.

        Two integrity checks guard against a parse break that still produces
        well-typed output. The parser pads short lists with zeros to exactly
        the port count, so a truncated statistics page arrives type-correct,
        length-correct and zeroed -- with every health signal green:

        - A list whose length disagrees with the port count (padding only
          extends, so this means EXTRA rows -- a header parsed as data, a
          model variant) is not trusted: None, and the collector decides.
        - Byte counters all reading zero right after a reading that was not
          zero is a parse break until proven otherwise, and raises. A
          reboot or a manual counter clear produces the same transition
          legitimately; those fail scrapes until rx/tx bytes move again --
          seconds on a switch passing traffic, and CRC movement alone does
          not count -- where trusting a broken parse costs silently flat
          traffic with ``netgear_plus_up`` still 1.
        """
        try:
            # Single upstream entry point: it picks the HTML or JSON-API path per
            # model and returns the parser output before any megabyte rounding.
            parsed = connector._get_port_statistics()  # noqa: SLF001
        except Exception:
            _LOGGER.warning(
                "raw counter path unavailable for %s; the collector decides "
                "whether to fall back or fail the scrape -- see "
                "netgear_plus_raw_counters and netgear_plus_up",
                self.host,
                exc_info=True,
            )
            return None
        if not isinstance(parsed, dict):
            return None
        wanted = ("sum_rx", "sum_tx", "crc_errors")
        if not all(isinstance(parsed.get(key), list) for key in wanted):
            return None
        ports = int(getattr(connector, "ports", 0) or 0)
        if ports and any(len(parsed[key]) != ports for key in wanted):
            mismatched_key = next(key for key in wanted if len(parsed[key]) != ports)
            _LOGGER.warning(
                "statistics row count disagrees with the port count for %s: "
                "%s has %d rows for %d ports; not trusting the raw path",
                self.host,
                mismatched_key,
                len(parsed[mismatched_key]),
                ports,
            )
            return None
        raw = {key: [int(value or 0) for value in parsed[key]] for key in wanted}
        if _bytes_went_all_zero(previous_raw, raw):
            msg = (
                f"all byte counters read zero on {self.host} right after a "
                "non-zero reading; a truncated statistics page is padded with "
                "zeros and looks exactly like this, so the scrape fails "
                "rather than exporting counters that may be a parse break "
                "(a real reboot or counter clear recovers automatically "
                "once rx/tx bytes move again)"
            )
            raise SwitchScrapeError(msg)
        return raw

    def scrape(self) -> dict[str, Any]:
        """Return a reading, reusing the cache while it is fresh."""
        with self._lock:
            now = time.monotonic()
            if self._cached is not None and (now - self._cached_at) < self.cache_seconds:
                return self._cached

            started = time.monotonic()
            if self._connector is None:
                self._connector = self._connect()
            try:
                infos = self._connector.get_switch_infos()
            except Exception:  # noqa: BLE001 - several unrelated types mean "poll failed"
                # The library refreshes an expired cookie in place, so reaching
                # here means the poll failed for some other reason -- a login it
                # could not recover, an unreachable switch, a page that would
                # not load or parse. Rebuild the session once before giving up.
                _LOGGER.info("reconnecting to %s after a failed poll", self.host)
                self._connector = self._connect()
                infos = self._connector.get_switch_infos()

            if not infos:
                msg = f"empty response from {self.host}"
                raise SwitchScrapeError(msg)

            reading = {
                "infos": infos,
                "raw": self._raw_port_statistics(
                    self._connector,
                    (self._cached or {}).get("raw"),
                ),
                "ports": int(getattr(self._connector, "ports", 0) or 0),
                "model": getattr(getattr(self._connector, "switch_model", None), "MODEL_NAME", ""),
                "duration": time.monotonic() - started,
            }
            self._cached = reading
            self._cached_at = time.monotonic()
            return reading


class NetgearSwitchCollector(Collector):
    """Translates one scrape into Prometheus metrics."""

    def __init__(self, scraper: NetgearSwitchScraper) -> None:
        self.scraper = scraper
        # None until the first successful scrape, then latched to the counter
        # derivation chosen there -- see _effective_raw.
        self._raw_latched: bool | None = None
        self._upgrade_logged = False
        # Serialises the latch check-and-set: without it, two concurrent
        # first-ever collects straddling a cache expiry could hold readings
        # that differ in raw availability and latch last-writer-wins --
        # after one of them already exported the losing derivation.
        self._latch_lock = threading.Lock()

    def collect(self) -> Iterator[Metric]:
        switch = self.scraper.name
        up = GaugeMetricFamily(
            "netgear_plus_up",
            "1 if the last scrape of the switch succeeded, 0 otherwise.",
            labels=["switch"],
        )
        try:
            reading = self.scraper.scrape()
            raw = self._effective_raw(reading["raw"])
        except Exception as exc:  # noqa: BLE001 - a dead switch must not 500 the endpoint
            _LOGGER.warning("scrape of %s failed: %s", self.scraper.host, exc)
            up.add_metric([switch], 0.0)
            yield up
            return

        up.add_metric([switch], 1.0)
        yield up
        yield from self._describe(reading, switch, raw)
        yield from self._ports(reading, switch, raw)

    def _effective_raw(self, raw: dict[str, list[int]] | None) -> dict[str, list[int]] | None:
        """Latch the counter derivation chosen on the first successful scrape.

        The two derivations differ by up to 5 kB, so switching between them
        mid-series corrupts the counter -- see the latch note in README.md.

        - raw latched, raw lost: the scrape fails (netgear_plus_up 0).
        - fallback latched, raw appears: fallback continues; a restart upgrades.
        - a failed scrape latches nothing.
        """
        with self._latch_lock:
            if self._raw_latched is None:
                self._raw_latched = raw is not None
                return raw
            if self._raw_latched and raw is None:
                msg = (
                    "raw counter path lost mid-run; failing the scrape "
                    "rather than switching the series to "
                    "differently-derived values"
                )
                raise SwitchScrapeError(msg)
            if not self._raw_latched and raw is not None:
                if not self._upgrade_logged:
                    self._upgrade_logged = True
                    _LOGGER.info(
                        "raw counter path became available for %s; restart the exporter to use it",
                        self.scraper.host,
                    )
                return None
            return raw

    def _describe(
        self, reading: dict[str, Any], switch: str, raw: dict[str, list[int]] | None
    ) -> Iterator[Metric]:
        infos = reading["infos"]

        duration = GaugeMetricFamily(
            "netgear_plus_scrape_duration_seconds",
            "Seconds spent collecting from the switch.",
            labels=["switch"],
        )
        duration.add_metric([switch], reading["duration"])
        yield duration

        # Reports the path the exported counters actually use -- the latched
        # one -- not whatever this reading happened to carry.
        raw_family = GaugeMetricFamily(
            "netgear_plus_raw_counters",
            "1 when byte counters come from the unrounded parser path, "
            "0 when they were reconstructed from rounded megabyte values.",
            labels=["switch"],
        )
        raw_family.add_metric([switch], 1.0 if raw is not None else 0.0)
        yield raw_family

        # Upstream sets response_time_s to `_start_time - _previous_timestamp`,
        # and resets _previous_timestamp only after a poll completes, so it is
        # the gap between polls rather than the time the switch took to answer.
        #
        # The first sample of any connection is not a poll interval at all:
        # _previous_timestamp is set in the connector's __init__, before
        # autodetect and login, so it spans those plus the metadata and
        # port-status fetches and the library's two 0.25s inter-request sleeps.
        # That happens once at process start and again whenever a failed poll
        # forces a reconnect -- but never on routine session expiry, which the
        # library handles by refreshing the cookie on the same connector.
        sample_interval = infos.get("response_time_s")
        if sample_interval is not None:
            interval = GaugeMetricFamily(
                "netgear_plus_library_sample_interval_seconds",
                "Seconds between the previous poll of the switch and this one, "
                "as measured by py-netgear-plus.",
                labels=["switch"],
            )
            interval.add_metric([switch], float(sample_interval))
            yield interval

        info = GaugeMetricFamily(
            "netgear_plus_switch_info",
            "Static switch facts; value is always 1.",
            labels=["switch", "model", "firmware", "bootloader", "serial", "ip"],
        )
        info.add_metric(
            [
                switch,
                str(reading["model"] or ""),
                str(infos.get("switch_firmware") or ""),
                str(infos.get("switch_bootloader") or ""),
                str(infos.get("switch_serial_number") or ""),
                str(infos.get("switch_ip") or ""),
            ],
            1.0,
        )
        yield info

    def _ports(
        self, reading: dict[str, Any], switch: str, raw: dict[str, list[int]] | None
    ) -> Iterator[Metric]:
        infos = reading["infos"]
        ports = reading["ports"] or self._infer_port_count(infos)

        # The port description is a LABEL on its own info metric rather than on
        # the numeric series: renaming a port in the switch UI would otherwise
        # orphan every existing time series for that port.
        port_info = GaugeMetricFamily(
            "netgear_plus_port_info",
            "Per-port metadata; value is always 1.",
            labels=["switch", "port", "description"],
        )
        status = GaugeMetricFamily(
            "netgear_plus_port_up",
            "1 when the port has link, 0 otherwise.",
            labels=["switch", "port"],
        )
        speed = GaugeMetricFamily(
            "netgear_plus_port_speed_mbps",
            "Negotiated link speed in Mbit/s (0 when the port is down).",
            labels=["switch", "port"],
        )
        rx = CounterMetricFamily(
            "netgear_plus_port_rx_bytes",
            "Bytes received on the port since the switch last reset its counters.",
            labels=["switch", "port"],
        )
        tx = CounterMetricFamily(
            "netgear_plus_port_tx_bytes",
            "Bytes transmitted on the port since the switch last reset its counters.",
            labels=["switch", "port"],
        )
        crc = CounterMetricFamily(
            "netgear_plus_port_crc_errors",
            "CRC error frames counted on the port. A rising value means a bad "
            "cable, connector or port -- not congestion. Absent when "
            "netgear_plus_raw_counters is 0.",
            labels=["switch", "port"],
        )

        for index in range(1, ports + 1):
            port = str(index)
            port_info.add_metric(
                [switch, port, str(infos.get(f"port_{index}_description") or "")], 1.0
            )
            status.add_metric(
                [switch, port],
                1.0 if infos.get(f"port_{index}_status") == "on" else 0.0,
            )
            speed.add_metric(
                [switch, port], float(infos.get(f"port_{index}_connection_speed") or 0)
            )

            rx_bytes, tx_bytes, crc_count = self._counters(infos, raw, index)
            if rx_bytes is not None:
                rx.add_metric([switch, port], rx_bytes)
            if tx_bytes is not None:
                tx.add_metric([switch, port], tx_bytes)
            if crc_count is not None:
                crc.add_metric([switch, port], crc_count)

        yield port_info
        yield status
        yield speed
        yield rx
        yield tx
        yield crc

    @staticmethod
    def _counters(
        infos: dict[str, Any], raw: dict[str, list[int]] | None, index: int
    ) -> tuple[float | None, float | None, float | None]:
        """Prefer unrounded parser values; fall back to rescaled megabytes."""
        if raw is not None:
            position = index - 1

            def at(key: str) -> float | None:
                values = raw.get(key) or []
                return float(values[position]) if position < len(values) else None

            return at("sum_rx"), at("sum_tx"), at("crc_errors")

        def rescaled(key: str) -> float | None:
            value = infos.get(f"port_{index}_{key}_mbytes")
            return None if value is None else float(value) * BYTES_PER_MEGABYTE

        # CRC is deliberately not exported here -- see the CRC note in README.md.
        return rescaled("sum_rx"), rescaled("sum_tx"), None

    @staticmethod
    def _infer_port_count(infos: dict[str, Any]) -> int:
        found = 0
        for key in infos:
            if key.startswith("port_") and key.endswith("_status"):
                try:
                    found = max(found, int(key.split("_")[1]))
                except (IndexError, ValueError):
                    continue
        return found


def read_password() -> str:
    """Read the switch password, preferring the *_FILE form used by secrets."""
    path = os.environ.get("NETGEAR_EXPORTER_PASSWORD_FILE")
    if path:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    password = os.environ.get("NETGEAR_EXPORTER_PASSWORD")
    if not password:
        msg = (
            "set NETGEAR_EXPORTER_PASSWORD or NETGEAR_EXPORTER_PASSWORD_FILE "
            "to the switch web-UI password"
        )
        raise SystemExit(msg)
    return password
