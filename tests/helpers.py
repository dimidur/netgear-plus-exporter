"""Fixtures and stubs shared by the three test modules.

Not a conftest: these are plain helpers called by name, and conftest only
injects fixtures. Nothing here touches a real switch.
"""

from __future__ import annotations

import logging

from netgear_plus_exporter import upstream
from netgear_plus_exporter.collector import NetgearSwitchCollector
from netgear_plus_exporter.scraper import NetgearSwitchScraper

# Every module logs under the package root, and tests assert on records from
# more than one of them.
PACKAGE_LOGGER = "netgear_plus_exporter"

PORTS = 5
# The address the scraper-level tests build against, asserted on by the ones
# that pin a message naming the switch.
SCRAPER_HOST = "192.0.2.1"
# What the collector-level stub reports, deliberately different from its
# metric label so tests can tell the two identifiers apart.
STUB_SWITCH = "test-switch"
STUB_HOST = "192.168.1.2"

RAW = {
    "sum_rx": [3_634_567, 0, 0, 61_234, 1_060_999],
    "sum_tx": [1_442_101, 0, 0, 3_400_000, 3_500_000],
    "crc_errors": [0, 0, 0, 7, 0],
}

# A poll timestamp from BEFORE a stub connector's, so the freshness guard sees
# a poll that completed and the retained data as current.
EARLIER_POLL = 0.0


def raw() -> dict[str, list[int]]:
    """Fresh lists every call: a shared constant would alias mutations, and an
    assertion comparing a mutated input against RAW could not fail."""
    return {key: list(values) for key, values in RAW.items()}


def zeros() -> dict[str, list[int]]:
    """Fresh lists every call: a shared constant would alias mutations."""
    return {key: [0] * PORTS for key in ("sum_rx", "sum_tx", "crc_errors")}


def infos(**overrides):
    values = {
        "switch_firmware": "V1.0.0.16",
        "switch_bootloader": "V1.0.0.2",
        "switch_serial_number": "ABC123",
        "switch_ip": STUB_HOST,
        "response_time_s": 1.5,
    }
    for port in range(1, PORTS + 1):
        values[f"port_{port}_status"] = "on" if port in (1, 4, 5) else "off"
        values[f"port_{port}_connection_speed"] = {1: 1000, 4: 100, 5: 100}.get(port, 0)
        values[f"port_{port}_description"] = ""
        values[f"port_{port}_sum_rx_mbytes"] = 3.63
        values[f"port_{port}_sum_tx_mbytes"] = 1.44
    # What upstream really emits: the last port only, plus the switch-wide sum.
    values[f"port_{PORTS}_crc_errors"] = 3
    values["sum_port_crc_errors"] = 3
    values.update(overrides)
    return values


def reading(**overrides):
    value = {
        "infos": infos(),
        "raw": None,
        "ports": PORTS,
        "model": "GS305E",
        "duration": 1.2,
    }
    value.update(overrides)
    return value


class StubSource:
    """Serves one reading forever, or a sequence with the last one repeating.

    An Exception in the sequence is raised instead of returned, which is how
    a failing scrape is modelled at the collector's boundary.
    """

    def __init__(self, readings):
        self._readings = list(readings) if isinstance(readings, list) else [readings]

    def scrape(self):
        value = self._readings.pop(0) if len(self._readings) > 1 else self._readings[0]
        if isinstance(value, Exception):
            raise value
        return value


class StubConnector:
    """What read_statistics reads, with no network.

    `_previous_data` and `_previous_timestamp` are the retained-poll pair the
    library updates together at the end of a successful poll. `polled_before`
    in the tests is the timestamp taken before that poll, so a stub whose
    timestamp differs is modelling a poll that completed.
    """

    def __init__(self, parsed, ports=PORTS, timestamp=1.0):
        self._previous_data = parsed
        self._previous_timestamp = timestamp
        self.ports = ports


class StubPollingConnector:
    """Enough connector for scrape() to run without any network.

    Models the library's contract: a poll that completes refreshes the
    retained statistics and the timestamp together, and one that returns
    early refreshes neither.

    It deliberately has no _get_port_statistics, so a second fetch would
    raise here rather than pass silently.
    """

    ports = PORTS
    switch_model = None

    def __init__(self, stats_sequence, *, completes=True):
        self._stats = stats_sequence
        self._completes = completes
        self._previous_data: dict = {}
        self._previous_timestamp = 0.0
        self.polls = 0

    def get_switch_infos(self):
        self.polls += 1
        if self._completes:
            self._previous_data = self._stats.pop(0) if len(self._stats) > 1 else self._stats[0]
            self._previous_timestamp += 1.0
        return {"switch_ip": SCRAPER_HOST}


def collector(*readings) -> NetgearSwitchCollector:
    """A collector fed a sequence of readings, one per collect() call."""
    return NetgearSwitchCollector(StubSource(list(readings)), switch=STUB_SWITCH, host=STUB_HOST)


def families(instance) -> dict:
    return {family.name: family for family in instance.collect()}


def collect(value) -> dict:
    return families(collector(value))


def sample(family, port):
    for entry in family.samples:
        if entry.labels.get("port") == str(port):
            return entry.value
    return None


def scraper() -> NetgearSwitchScraper:
    # No I/O happens in __init__; the connection is only built inside scrape().
    return NetgearSwitchScraper(SCRAPER_HOST, "unused")


def scraper_failing_with(poll_exc, monkeypatch, connect_exc=None) -> NetgearSwitchScraper:
    """A scraper whose first poll raises, and whose reconnect raises or works.

    `connect_exc` None means the replacement connector polls normally, which
    is the recovery path; an exception means the reconnect fails too.
    """
    instance = NetgearSwitchScraper(SCRAPER_HOST, "unused", cache_seconds=0.0)

    class _PollRaises(StubPollingConnector):
        def get_switch_infos(self):
            raise poll_exc

    instance._connector = _PollRaises([raw()])

    def _connect():
        if connect_exc is not None:
            raise connect_exc
        return StubPollingConnector([raw()])

    monkeypatch.setattr(instance, "_connect", _connect)
    return instance


def read_statistics(connector, previous=None, owner=None):
    """upstream.read_statistics, wired to a scraper's degraded-warning latch.

    The two are separate modules and separately testable; this pairs them the
    way the scraper does, so the latch tests see the real behaviour.
    """
    holder = owner or scraper()
    result, degraded = upstream.read_statistics(connector, EARLIER_POLL, previous, holder.host)
    if degraded is not None:
        holder._warn_degraded(degraded)
    return result


def records_starting(caplog, prefix) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.getMessage().startswith(prefix)]


def record_starting(caplog, prefix) -> logging.LogRecord:
    found = records_starting(caplog, prefix)
    assert found, f"no log record starting {prefix!r} in {caplog.text!r}"
    return found[0]
