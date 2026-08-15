"""Tests for the metric translation layer.

These deliberately avoid touching a real switch: the collector is fed canned
readings so the parts that are easy to get subtly wrong -- counter selection,
rounding fallback, port-count inference -- are pinned down.
"""

from __future__ import annotations

import logging

import pytest

from netgear_plus_exporter import exporter
from netgear_plus_exporter.exporter import (
    NetgearSwitchCollector,
    NetgearSwitchScraper,
    SwitchScrapeError,
    _bytes_went_all_zero,
)

PORTS = 5


def _infos(**overrides):
    infos = {
        "switch_firmware": "V1.0.0.16",
        "switch_bootloader": "V1.0.0.2",
        "switch_serial_number": "ABC123",
        "switch_ip": "192.168.1.2",
        "response_time_s": 1.5,
    }
    for port in range(1, PORTS + 1):
        infos[f"port_{port}_status"] = "on" if port in (1, 4, 5) else "off"
        infos[f"port_{port}_connection_speed"] = {1: 1000, 4: 100, 5: 100}.get(port, 0)
        infos[f"port_{port}_description"] = ""
        infos[f"port_{port}_sum_rx_mbytes"] = 3.63
        infos[f"port_{port}_sum_tx_mbytes"] = 1.44
    # What upstream really emits: the last port only, plus the switch-wide sum.
    infos[f"port_{PORTS}_crc_errors"] = 3
    infos["sum_port_crc_errors"] = 3
    infos.update(overrides)
    return infos


class _StubScraper:
    """Serves one reading forever, or a sequence with the last one repeating."""

    name = "test-switch"
    host = "192.168.1.2"

    def __init__(self, reading):
        self._readings = list(reading) if isinstance(reading, list) else [reading]

    def scrape(self):
        reading = self._readings.pop(0) if len(self._readings) > 1 else self._readings[0]
        if isinstance(reading, Exception):
            raise reading
        return reading


def _collect(reading):
    return _families(_collector(reading))


def _sample(family, port):
    for sample in family.samples:
        if sample.labels.get("port") == str(port):
            return sample.value
    return None


def _reading(**overrides):
    reading = {
        "infos": _infos(),
        "raw": None,
        "ports": PORTS,
        "model": "GS305E",
        "duration": 1.2,
    }
    reading.update(overrides)
    return reading


RAW = {
    "sum_rx": [3_634_567, 0, 0, 61_234, 1_060_999],
    "sum_tx": [1_442_101, 0, 0, 3_400_000, 3_500_000],
    "crc_errors": [0, 0, 0, 7, 0],
}


def _raw():
    """Fresh lists every call: a shared constant would alias mutations, and an
    assertion comparing a mutated input against RAW could not fail."""
    return {key: list(values) for key, values in RAW.items()}


def _zeros():
    """Fresh lists every call: a shared constant would alias mutations."""
    return {key: [0] * PORTS for key in exporter._COUNTER_KEYS}


def _collector(*readings):
    """A collector fed a sequence of readings, one per collect() call."""
    return NetgearSwitchCollector(_StubScraper(list(readings)))


def _families(collector):
    return {family.name: family for family in collector.collect()}


def test_raw_parser_values_win_over_rounded_megabytes():
    # Arrange + Act
    families = _collect(_reading(raw=RAW))

    # Assert -- exact byte counts, not the 10 kB-quantised 3.63 MB the library reports.
    assert _sample(families["netgear_plus_port_rx_bytes"], 1) == 3_634_567
    # CRC is present for every port, including ports the public dict omits.
    assert _sample(families["netgear_plus_port_crc_errors"], 4) == 7
    assert _sample(families["netgear_plus_port_crc_errors"], 1) == 0
    # Port 5 discriminates the two sources: the parser says 0, the public
    # dict's delta says 3. The raw path must report the parser.
    assert _sample(families["netgear_plus_port_crc_errors"], 5) == 0
    # Switch-scoped, not per-port; _sample() matches on a "port" label.
    assert "port" not in families["netgear_plus_raw_counters"].samples[0].labels
    assert families["netgear_plus_raw_counters"].samples[0].value == 1.0


def test_sample_interval_is_named_and_described_for_what_it_measures():
    """response_time_s is the gap between polls, so neither half of the old
    name held: it is not switch-reported and it is not a response time.

    The name alone is not enough -- a HELP string can reintroduce the same
    false claim under a correct name -- so the description is pinned too.
    """
    # Arrange + Act
    families = _collect(_reading())

    # Assert
    assert "netgear_plus_switch_response_seconds" not in families
    interval = families["netgear_plus_library_sample_interval_seconds"]
    assert interval.samples[0].value == 1.5
    # Pinned whole: a keyword check passes for a reworded version of the same
    # false claim.
    assert interval.documentation == (
        "Seconds between the previous poll of the switch and this one, "
        "as measured by py-netgear-plus."
    )


def test_sample_interval_is_omitted_when_upstream_does_not_report_it():
    """The guard is defensive -- 0.6.4 always sets the key -- but a future
    release dropping it must skip the metric, not export None as a float.
    """
    # Arrange
    infos = _infos()
    del infos["response_time_s"]

    # Act
    families = _collect(_reading(infos=infos))

    # Assert
    assert "netgear_plus_library_sample_interval_seconds" not in families


def test_sample_interval_of_zero_is_a_real_sample():
    """Zero is a value, not an absence, so the guard tests `is not None`.

    Nothing in normal operation produces it -- the library sleeps 0.25s twice
    per poll, putting a 0.5s floor under the interval -- so this is purely
    defensive. A truthiness check would drop the sample instead of exporting it.
    """
    # Arrange + Act
    families = _collect(_reading(infos=_infos(response_time_s=0)))

    # Assert
    interval = families["netgear_plus_library_sample_interval_seconds"]
    assert interval.samples[0].value == 0.0


def test_falls_back_to_rescaled_megabytes_when_parser_unavailable():
    # Arrange + Act
    families = _collect(_reading())

    # Assert
    assert _sample(families["netgear_plus_port_rx_bytes"], 1) == 3_630_000
    assert families["netgear_plus_raw_counters"].samples[0].value == 0.0
    # The fixture carries port_5_crc_errors, so this fails if it is exported.
    assert families["netgear_plus_port_crc_errors"].samples == []


def test_fallback_never_exports_the_per_interval_crc_delta():
    """The key is a delta, so it stays out even if upstream ever fixes the
    one-port-only bug and populates every port.
    """
    # Arrange
    infos = _infos()
    for port in range(1, PORTS + 1):
        infos[f"port_{port}_crc_errors"] = 999
    infos["sum_port_crc_errors"] = 999 * PORTS

    # Act
    families = _collect(_reading(infos=infos))

    # Assert
    crc = families["netgear_plus_port_crc_errors"]
    assert crc.samples == []
    # The empty family is all a /metrics reader sees, so the HELP has to say
    # the absence is deliberate.
    assert "Absent when netgear_plus_raw_counters is 0." in crc.documentation


def test_link_state_and_speed_are_reported_per_port():
    # Arrange + Act
    families = _collect(_reading())

    # Assert
    assert _sample(families["netgear_plus_port_up"], 1) == 1.0
    assert _sample(families["netgear_plus_port_up"], 2) == 0.0
    assert _sample(families["netgear_plus_port_speed_mbps"], 4) == 100.0
    assert _sample(families["netgear_plus_port_speed_mbps"], 2) == 0.0


def test_description_is_a_label_on_its_own_info_metric():
    # Arrange
    infos = _infos()
    infos["port_4_description"] = "doorbell-camera"

    # Act
    families = _collect(_reading(infos=infos))

    # Assert
    port_info = families["netgear_plus_port_info"]
    described = [sample for sample in port_info.samples if sample.labels["port"] == "4"]
    assert described[0].labels["description"] == "doorbell-camera"
    # The numeric series must NOT carry the description, or renaming a port
    # orphans its history.
    speed_labels = families["netgear_plus_port_speed_mbps"].samples[0].labels
    assert "description" not in speed_labels


def test_losing_the_raw_path_fails_the_scrape_instead_of_switching():
    """Losing the raw path fails the scrape rather than switching derivations
    -- see the latch note in README.md for why a switch corrupts the series.
    """
    # Arrange
    collector = _collector(
        _reading(raw=RAW),
        _reading(),  # raw lost: parser layer stopped answering
        _reading(raw=RAW),  # and back
    )

    # Act + Assert -- three rounds; each collect is one act on the latch.
    first = _families(collector)
    assert first["netgear_plus_up"].samples[0].value == 1.0
    assert _sample(first["netgear_plus_port_rx_bytes"], 1) == 3_634_567

    # The flip is refused: down, and no counters -- NOT 3_630_000 from the
    # fallback derivation into the same series.
    lost = _families(collector)
    assert lost["netgear_plus_up"].samples[0].value == 0.0
    assert "netgear_plus_port_rx_bytes" not in lost

    # A refusal, not a poisoning: the raw path coming back resumes normally.
    recovered = _families(collector)
    assert recovered["netgear_plus_up"].samples[0].value == 1.0
    assert _sample(recovered["netgear_plus_port_rx_bytes"], 1) == 3_634_567


def test_fallback_stays_latched_when_raw_appears():
    """The upgrade direction is deferred to a restart: jumping to raw
    mid-series is the same discontinuity as the other direction, mirrored.
    """
    # Arrange
    collector = _collector(_reading(), _reading(raw=RAW))

    # Act + Assert -- two rounds; the second is where an upgrade could flip.
    first = _families(collector)
    assert _sample(first["netgear_plus_port_rx_bytes"], 1) == 3_630_000

    upgraded = _families(collector)
    assert upgraded["netgear_plus_up"].samples[0].value == 1.0
    assert _sample(upgraded["netgear_plus_port_rx_bytes"], 1) == 3_630_000
    # raw_counters reports the path the EXPORTED counters use, which is still
    # the fallback -- not what the reading happened to carry.
    assert upgraded["netgear_plus_raw_counters"].samples[0].value == 0.0


def test_upgrade_notice_is_logged_exactly_once(caplog):
    """While fallback is latched and raw is available, the exporter logs that
    a restart would upgrade -- once. Unguarded, the notice would repeat on
    every collect: ~2,900 lines per day at a 30s scrape interval, forever.
    """
    # Arrange
    collector = _collector(_reading(), _reading(raw=RAW))

    # Act -- two rounds in the upgrade condition.
    with caplog.at_level(logging.INFO, logger=exporter._LOGGER.name):
        _families(collector)
        _families(collector)
        _families(collector)

    # Assert
    notices = [
        record for record in caplog.records if "raw counter path became available" in record.message
    ]
    assert len(notices) == 1


def test_failed_scrape_does_not_latch_a_path():
    """A switch that is unreachable at startup must still get the raw path
    once it answers: only a successful scrape chooses the derivation.
    """
    # Arrange
    collector = _collector(RuntimeError("switch unreachable"), _reading(raw=RAW))

    # Act + Assert -- the failure must not choose a path for round two.
    down = _families(collector)
    assert down["netgear_plus_up"].samples[0].value == 0.0

    first_good = _families(collector)
    assert first_good["netgear_plus_raw_counters"].samples[0].value == 1.0
    assert _sample(first_good["netgear_plus_port_rx_bytes"], 1) == 3_634_567


def test_failed_scrape_reports_down_instead_of_raising():
    # Arrange + Act
    families = _collect(RuntimeError("switch unreachable"))

    # Assert
    assert families["netgear_plus_up"].samples[0].value == 0.0
    assert "netgear_plus_port_up" not in families


def test_port_count_is_inferred_when_connector_does_not_report_it():
    # Arrange + Act
    families = _collect(_reading(ports=0, model=""))

    # Assert
    assert len(families["netgear_plus_port_up"].samples) == PORTS


def test_upstream_private_attributes_still_exist():
    """Guards the binding to non-public upstream state.

    Unrounded counters and per-port CRC come from the statistics the library
    retains after its own poll, and whether that data belongs to this poll is
    judged by the timestamp it refreshes alongside. If a py-netgear-plus
    release renames or drops either, the exporter degrades to rounded
    megabyte values and no CRC at all, which is exactly the failure this
    project exists to avoid, and it is invisible without a switch to test
    against. This catches the rename in CI, with no hardware.

    Both are set in __init__, so the class-level check is on an instance.
    """
    # Arrange
    import py_netgear_plus

    connector = py_netgear_plus.NetgearSwitchConnector("192.0.2.1", "unused")

    # Act + Assert -- the names survive...
    assert hasattr(connector, "_previous_data")
    assert hasattr(connector, "_previous_timestamp")

    # ...and _previous_data still carries the three counter lists. A release
    # that kept the attribute but moved the statistics elsewhere would pass a
    # name check while silently dropping this exporter to rounded megabytes.
    connector._set_instance_attributes_by_model(py_netgear_plus.models.GS305E)
    for key in ("sum_rx", "sum_tx", "crc_errors"):
        assert isinstance(connector._previous_data[key], list)


def test_upstream_still_assigns_the_retained_pair_after_the_supported_return():
    """Pins the source ordering the freshness guard depends on.

    `_raw_port_statistics` treats an unmoved `_previous_timestamp` as proof
    that the retained data is from an earlier poll. That holds only while
    upstream assigns the timestamp and the data together, after the early
    return for an unsupported model. Hoisting the timestamp above that return
    is a plausible refactor, since upstream reads it only for `sample_time`,
    and it would turn the guard into a no-op that exports a zero-filled
    reseed as live data.

    Reads source text, so it is brittle by design: a bump that reformats this
    method should fail here and be re-verified by hand.
    """
    # Arrange
    import inspect

    import py_netgear_plus

    lines = [
        line.strip()
        for line in inspect.getsource(
            py_netgear_plus.NetgearSwitchConnector.get_switch_infos
        ).splitlines()
    ]

    # Act
    supported_guard = lines.index("if not self.switch_model.SUPPORTED:")
    timestamp_assignment = next(
        index for index, line in enumerate(lines) if line.startswith("self._previous_timestamp =")
    )

    # Assert
    assert lines[supported_guard + 1] == "return switch_data"
    assert timestamp_assignment > supported_guard
    assert lines[timestamp_assignment + 1].startswith("self._previous_data =")


class _StubConnector:
    """What _raw_port_statistics reads, with no network.

    `_previous_data` and `_previous_timestamp` are the retained-poll pair the
    library updates together at the end of a successful poll. `polled_before`
    in the tests is the timestamp taken before that poll, so a stub whose
    timestamp differs is modelling a poll that completed.
    """

    def __init__(self, parsed, ports=PORTS, timestamp=1.0):
        self._previous_data = parsed
        self._previous_timestamp = timestamp
        self.ports = ports


# A poll timestamp from BEFORE the stub's, so the guard sees a poll that
# completed and the retained data as current.
EARLIER_POLL = 0.0


def _scraper():
    # No I/O happens in __init__; the connection is only built inside scrape().
    return NetgearSwitchScraper("192.0.2.1", "unused")


def test_each_degraded_reason_warns_once_then_drops_to_debug(caplog):
    """A condition that degrades a scrape degrades every scrape. Unlatched,
    that is thousands of identical WARNING lines a day at a 30s interval.

    The latch is per reason, not per scraper: the third call takes a different
    branch and must warn, even though an earlier reason has already latched.
    """
    # Arrange -- one stub with no counter lists, one with too many rows.
    scraper = _scraper()
    no_lists = _StubConnector({"something": "else"})
    extra_rows = _StubConnector({key: [0] * (PORTS + 1) for key in RAW})

    # Act -- one reason twice, then a second reason twice.
    with caplog.at_level(logging.DEBUG, logger=exporter._LOGGER.name):
        scraper._raw_port_statistics(no_lists, EARLIER_POLL, None)
        scraper._raw_port_statistics(no_lists, EARLIER_POLL, None)
        scraper._raw_port_statistics(extra_rows, EARLIER_POLL, None)
        scraper._raw_port_statistics(extra_rows, EARLIER_POLL, None)

    # Assert -- filtered by logger, since py_netgear_plus logs on its own.
    # The fourth call is what proves the second reason latched rather than
    # merely warned once.
    levels = [record.levelname for record in caplog.records if record.name == exporter._LOGGER.name]
    assert levels == ["WARNING", "DEBUG", "WARNING", "DEBUG"]


def test_an_empty_retained_dict_is_rejected_as_unavailable():
    """`_previous_data` is `{}` between `__init__` and `autodetect_model()`,
    and an empty dict passes an isinstance check.

    The exporter's own `_connect()` always autodetects, so this shape is not
    reachable through it today: it guards an upstream change that leaves the
    dict empty after a poll.

    Both this and the missing-lists branch return None and log, so asserting
    only "None and something was logged" cannot tell them apart and passes
    even with the emptiness check removed. The reason is what is pinned: an
    empty dict means the path is unavailable, not that the payload arrived
    malformed.
    """
    # Arrange -- a connector whose poll never completed.
    scraper = _scraper()
    never_polled = _StubConnector({})

    # Act
    raw = scraper._raw_port_statistics(never_polled, EARLIER_POLL, None)

    # Assert -- pinned to the reason constant, so rewording the message
    # cannot break this and a message that merely resembles it cannot pass.
    assert raw is None
    assert scraper._warned == {exporter._REASON_NO_RETAINED_DATA}


def test_statistics_with_extra_rows_are_not_trusted():
    """The parser pads SHORT lists to the port count, so a length mismatch
    can only mean extra rows -- a header parsed as data, a model variant.
    Positionally shifted counters attributed to the wrong ports are worse
    than the rounded fallback.
    """
    # Arrange
    parsed = {
        "sum_rx": [1, 2, 3, 4, 5, 6],  # six rows for a five-port switch
        "sum_tx": [1, 2, 3, 4, 5, 6],
        "crc_errors": [0, 0, 0, 0, 0, 0],
    }

    # Act
    raw = _scraper()._raw_port_statistics(_StubConnector(parsed), EARLIER_POLL, None)

    # Assert
    assert raw is None


def test_correct_length_statistics_pass_the_guard():
    # Arrange
    parsed = _raw()

    # Act
    raw = _scraper()._raw_port_statistics(_StubConnector(parsed), EARLIER_POLL, None)

    # Assert
    assert raw == RAW


def test_length_guard_is_skipped_when_the_port_count_is_unknown():
    """ports=0 means the connector never learned the count; rejecting on a
    comparison against 0 would reject every reading.
    """
    # Arrange
    parsed = _raw()

    # Act
    raw = _scraper()._raw_port_statistics(_StubConnector(parsed, ports=0), EARLIER_POLL, None)

    # Assert
    assert raw == RAW


def test_all_zero_bytes_after_traffic_fail_the_scrape():
    """A truncated statistics page arrives type-correct, length-correct and
    zero-padded -- indistinguishable from a healthy idle switch except for
    history. Counters do not all reset while traffic was flowing, so the
    scrape fails loudly instead of exporting flat zeros with up=1.
    """
    # Arrange
    connector = _StubConnector(_zeros())

    # Act + Assert
    with pytest.raises(SwitchScrapeError, match="parse break"):
        _scraper()._raw_port_statistics(connector, EARLIER_POLL, RAW)


def test_all_zero_bytes_with_no_history_are_a_valid_reading():
    # Arrange -- a brand-new idle switch really does read all zeros.
    zeros = _zeros()

    # Act
    raw = _scraper()._raw_port_statistics(_StubConnector(zeros), EARLIER_POLL, None)

    # Assert
    assert raw == zeros


def test_zero_transition_ignores_crc_history():
    """CRC-only history must not arm the trap: all-zero CRC is the healthy
    steady state, so it proves nothing about whether bytes were flowing.
    """
    # Arrange
    crc_only_history = dict(_zeros(), crc_errors=[3, 0, 0, 0, 0])
    crc_only_now = dict(_zeros(), crc_errors=[9, 0, 0, 0, 0])

    # Act + Assert -- CRC on the history side proves nothing...
    assert _bytes_went_all_zero(crc_only_history, _zeros()) is False
    assert _bytes_went_all_zero(RAW, _zeros()) is True
    assert _bytes_went_all_zero(_zeros(), _zeros()) is False
    assert _bytes_went_all_zero(None, _zeros()) is False
    # ...and CRC on the current side buys no amnesty: a partially truncated
    # page can keep a nonzero CRC cell while every byte counter zeroes out,
    # which is exactly the parse break this exists to catch.
    assert _bytes_went_all_zero(RAW, crc_only_now) is True


class _StubPollingConnector:
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
        return {"switch_ip": "192.0.2.1"}


def test_one_scrape_polls_the_switch_once():
    """One poll per scrape, not two. See docs/own-the-protocol.md for why a
    duplicate fetch is a real cost on this hardware.

    The stub has no _get_port_statistics, so a second fetch raises rather
    than passing quietly.
    """
    # Arrange
    scraper = NetgearSwitchScraper("192.0.2.1", "unused", cache_seconds=0.0)
    connector = _StubPollingConnector([_raw()])
    scraper._connector = connector

    # Act
    reading = scraper.scrape()

    # Assert -- one poll, and the counters came from it.
    assert connector.polls == 1
    assert reading["raw"] == RAW


def test_an_incomplete_poll_does_not_report_stale_statistics_as_current():
    """A partially supported model makes get_switch_infos() return before it
    refreshes either the retained statistics or the timestamp. Reporting the
    previous poll's counters as this one's would be silent and wrong, so the
    unmoved timestamp drops the raw path instead.
    """
    # Arrange
    scraper = NetgearSwitchScraper("192.0.2.1", "unused", cache_seconds=0.0)
    connector = _StubPollingConnector([_raw()], completes=False)
    connector._previous_data = _raw()  # left over from an earlier poll
    scraper._connector = connector

    # Act
    reading = scraper.scrape()

    # Assert
    assert reading["raw"] is None


def test_reconnect_reads_the_timestamp_from_the_replacement_connector(monkeypatch):
    """time.perf_counter() is process-wide, so a timestamp taken from the
    connector that failed is still comparable against the one that replaced
    it, and that is the trap: a fresh connector's __init__ stamp is always
    greater than the old connector's, so an early-returning poll on the new
    one would pass the guard and publish its zero-seeded retained data as a
    live reading.

    Deleting the reassignment in scrape() turns this red.
    """
    # Arrange -- first connector fails, replacement never completes a poll.
    scraper = NetgearSwitchScraper("192.0.2.1", "unused", cache_seconds=0.0)

    class _FailingConnector(_StubPollingConnector):
        def get_switch_infos(self):
            raise RuntimeError("poll failed")

    failing = _FailingConnector([_raw()])
    failing._previous_timestamp = 100.0
    scraper._connector = failing

    replacement = _StubPollingConnector([_raw()], completes=False)
    replacement._previous_timestamp = 500.0  # a later __init__ stamp
    # Zero-filled lists are what _set_instance_attributes_by_model seeds,
    # reached via autodetect_model() during connect. __init__ alone leaves
    # _previous_data an empty dict.
    replacement._previous_data = _zeros()
    monkeypatch.setattr(scraper, "_connect", lambda: replacement)

    # Act
    reading = scraper.scrape()

    # Assert -- the incomplete poll is not reported as a reading.
    assert reading["raw"] is None


def test_upstream_repairs_negative_bytes_and_leaves_crc_alone():
    """Counters now come from the data the library retains, which is
    post-repair. This pins the two halves of that contract, in memory and
    without hardware, because a release that started writing back into the
    crc_errors list would corrupt CRC with every gate still green.
    """
    # Arrange
    import py_netgear_plus

    connector = py_netgear_plus.NetgearSwitchConnector("192.0.2.1", "unused")
    connector.ports = 2
    # The keys _update_current_data reads, seeded as a real poll would.
    connector._previous_data = {
        "sum_rx": [10, 20],
        "sum_tx": [30, 40],
        "crc_errors": [1, 2],
        "traffic_rx": [0, 0],
        "traffic_tx": [0, 0],
    }
    # Seeded by the library itself, then given the parser's own keys, so the
    # fixture cannot drift from what a real poll builds.
    current = connector._initialize_current_data()
    current.update(
        {
            "sum_rx": [-1, 500],  # negative on a connected port
            "sum_tx": [30, 40],
            "crc_errors": [5, 6],
            "traffic_rx": [0, 0],
            "traffic_tx": [0, 0],
            "speed_io": [0, 0],
        }
    )
    switch_data = {"port_1_status": "on", "port_2_status": "on"}

    # Act
    connector._update_current_data(current, switch_data, 1.0)

    # Assert -- the negative is replaced by the previous value...
    assert current["sum_rx"][0] == 10
    # ...and CRC comes through byte-identical, which is why the list is read
    # instead of the per-port delta the public dict carries.
    assert current["crc_errors"] == [5, 6]


def test_scrape_wires_cached_history_and_preserves_it_on_failure():
    """The zero-transition guard only works if scrape() hands it the cached
    previous reading AND a failed scrape leaves that cache untouched. Moving
    the cache write above the guard would silently break recovery: the zeroed
    reading would become the new history and the next zeros would pass.
    """
    # Arrange -- connection pre-seeded, so no network is involved.
    scraper = NetgearSwitchScraper("192.0.2.1", "unused", cache_seconds=0.0)
    scraper._connector = _StubPollingConnector([_raw(), _zeros(), _zeros()])

    # Act + Assert -- traffic, then zeros, then zeros again.
    assert scraper.scrape()["raw"] == RAW
    with pytest.raises(SwitchScrapeError, match="parse break"):
        scraper.scrape()
    # History survived the failed scrape: still compared against RAW.
    with pytest.raises(SwitchScrapeError, match="parse break"):
        scraper.scrape()
