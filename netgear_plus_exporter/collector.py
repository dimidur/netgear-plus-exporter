"""One reading, translated into Prometheus metrics.

Changes when the metric contract changes. It does not import the upstream
binding, so a change to the library's API stops there. It does still read the
switch's own key names out of the reading, so a firmware that renames one is
an edit here too.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from typing import Any, Protocol

from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily, Metric
from prometheus_client.registry import Collector

from .errors import SwitchScrapeError

_LOGGER = logging.getLogger(__name__)

BYTES_PER_MEGABYTE = 1_000_000  # py_netgear_plus uses decimal MB (1e-6)


class Source(Protocol):
    """Where a reading comes from. One method, so a test double is one method."""

    def scrape(self) -> dict[str, Any]: ...


class NetgearSwitchCollector(Collector):
    """Translates one scrape into Prometheus metrics.

    ``switch`` is the value of the ``switch`` label and ``host`` the address
    to name in a log line. Both are passed rather than read off the source:
    a failure has to be reported before any reading exists, and identity is a
    presentation concern the poller has no use for.
    """

    def __init__(self, source: Source, switch: str, host: str) -> None:
        self.source = source
        self.switch = switch
        self.host = host
        # None until the first successful scrape, then the derivation it used.
        self._raw_latched: bool | None = None
        self._upgrade_logged = False
        # Serialises both latches against concurrent collects. start_http_server
        # is threaded, so simultaneous collect() calls are the normal case.
        self._latch_lock = threading.Lock()
        # The fault the previous collect logged, or None after a success.
        self._last_failure: str | None = None

    def collect(self) -> Iterator[Metric]:
        switch = self.switch
        up = GaugeMetricFamily(
            "netgear_plus_up",
            "1 when this scrape produced a full reading from the switch, 0 otherwise.",
            labels=["switch"],
        )
        try:
            reading = self.source.scrape()
            raw = self._effective_raw(reading["raw"])
            # Built inside the try, not yielded from it. A generator body runs
            # during registry iteration, which is after this function returns,
            # so anything raised while constructing a metric would reach
            # generate_latest as a 500 and discard the up sample below.
            metrics = [
                *self._describe(reading, switch, raw),
                *self._ports(reading, switch, raw),
            ]
        except Exception as exc:  # noqa: BLE001 - a dead switch must not 500 the endpoint
            # Both identifiers, because a library exception reaching here names
            # neither: the label matches the up 0 sample, the host says which
            # address failed.
            _LOGGER.warning(
                "scrape of %s (%s) failed: %r",
                switch,
                self.host,
                exc,
                exc_info=self._first_sighting(exc),
            )
            up.add_metric([switch], 0.0)
            yield up
            return

        with self._latch_lock:
            self._last_failure = None
        up.add_metric([switch], 1.0)
        yield up
        yield from metrics

    def _first_sighting(self, exc: Exception) -> bool:
        """True the first time a fault is seen, False while it repeats.

        Only the traceback is suppressed; the WARNING always goes out. A fault
        is the type at the root of the cause chain: the root because one fault
        arrives twice, as itself and as the wrapper the scraper replays.

        The exporter raises one type for every fault of its own, so those are
        told apart by message as well. Those strings are written here, not by
        the library, so nothing outside this package can make the key unstable
        the way a third-party repr with an address in it would.
        """
        root: BaseException = exc
        while root.__cause__ is not None:
            root = root.__cause__
        seen = type(root).__qualname__
        if isinstance(root, SwitchScrapeError):
            seen = f"{seen}: {root}"
        with self._latch_lock:
            first = seen != self._last_failure
            self._last_failure = seen
        return first

    def _effective_raw(self, raw: dict[str, list[int]] | None) -> dict[str, list[int]] | None:
        """Latch the counter derivation chosen on the first successful scrape.

        - raw latched, raw lost: the scrape fails (netgear_plus_up 0).
        - fallback latched, raw appears: fallback continues; a restart upgrades.
        - a scrape that fails before reaching here latches nothing. One that
          fails after, while building metrics, has already latched.
        """
        with self._latch_lock:
            if self._raw_latched is None:
                self._raw_latched = raw is not None
                return raw
            if self._raw_latched and raw is None:
                msg = (
                    f"raw counter path for {self.host} lost mid-run; "
                    "failing the scrape rather than switching the series to "
                    "differently-derived values"
                )
                raise SwitchScrapeError(msg)
            if not self._raw_latched and raw is not None:
                if not self._upgrade_logged:
                    self._upgrade_logged = True
                    _LOGGER.info(
                        "raw counter path became available for %s; restart the exporter to use it",
                        self.host,
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
        # Every add_metric value passes through float() inside collect()'s try.
        # generate_latest casts again afterwards, where a failure is a 500
        # rather than an up 0.
        duration.add_metric([switch], float(reading["duration"]))
        yield duration

        # Reports the latched path, not whatever this reading carried.
        raw_family = GaugeMetricFamily(
            "netgear_plus_raw_counters",
            "1 when byte counters come from the unrounded parser path, "
            "0 when they were reconstructed from rounded megabyte values.",
            labels=["switch"],
        )
        raw_family.add_metric([switch], 1.0 if raw is not None else 0.0)
        yield raw_family

        # Upstream sets response_time_s to `_start_time - _previous_timestamp`,
        # so it is the gap between polls, not the switch's response time. The
        # first sample of a connection instead spans autodetect and login,
        # which recurs whenever a failed poll forces a reconnect.
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

            def value_at(key: str) -> float | None:
                values = raw.get(key) or []
                return float(values[position]) if position < len(values) else None

            return value_at("sum_rx"), value_at("sum_tx"), value_at("crc_errors")

        def rescaled(key: str) -> float | None:
            value = infos.get(f"port_{index}_{key}_mbytes")
            return None if value is None else float(value) * BYTES_PER_MEGABYTE

        # No CRC on the fallback path.
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
