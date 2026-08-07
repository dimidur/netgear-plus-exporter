"""Tests for the metric translation layer.

These deliberately avoid touching a real switch: the collector is fed canned
readings so the parts that are easy to get subtly wrong -- counter selection,
rounding fallback, port-count inference -- are pinned down.
"""

from __future__ import annotations

from netgear_plus_exporter.exporter import NetgearSwitchCollector

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
    # Switch-scoped, not per-port: _sample() matches on a "port" label, so the
    # previous form here compared against the string "None" and could not fail.
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
    # Pinned whole rather than by keyword: substring checks measure vocabulary,
    # not meaning. "Seconds the switch took to answer the poll, as measured by
    # py-netgear-plus" satisfies any plausible set of keyword assertions while
    # restating exactly the claim this metric was renamed to stop making.
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
    # No CRC is available on the fallback path; better absent than fabricated.
    assert families["netgear_plus_port_crc_errors"].samples == []


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
    """The two counter paths disagree by up to 5 kB -- half the fallback's
    rounding quantum -- so flipping between them mid-series can move a counter
    DOWN.
    Prometheus reads that as a counter reset and increase() credits the whole
    counter as new traffic -- corruption that outlives the flip. Losing the
    raw path must therefore fail the scrape, not switch derivations.
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
    """The upgrade direction is deferred to a restart rather than taken live:
    the byte series keep their derivation and stay monotone, because jumping
    to raw mid-series is the same up-to-5kB discontinuity as the other
    direction, just mirrored.
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
    import logging

    collector = _collector(_reading(), _reading(raw=RAW))

    # Act -- two rounds in the upgrade condition.
    with caplog.at_level(logging.INFO, logger="netgear_plus_exporter.exporter"):
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


def test_upstream_private_entry_point_still_exists():
    """Guards the one binding to a non-public upstream API.

    Unrounded counters and per-port CRC both come from
    NetgearSwitchConnector._get_port_statistics(). If a py-netgear-plus release
    renames or removes it, the exporter silently degrades to rounded megabyte
    values and partial CRC -- which is exactly the failure this project exists
    to avoid, and it is invisible without a switch to test against. This catches
    the rename in CI, with no hardware.
    """
    # Arrange
    import py_netgear_plus

    # Act + Assert
    assert hasattr(py_netgear_plus.NetgearSwitchConnector, "_get_port_statistics")
