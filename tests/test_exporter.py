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
    name = "test-switch"
    host = "192.168.1.2"

    def __init__(self, reading):
        self._reading = reading

    def scrape(self):
        if isinstance(self._reading, Exception):
            raise self._reading
        return self._reading


def _collect(reading):
    families = list(NetgearSwitchCollector(_StubScraper(reading)).collect())
    return {family.name: family for family in families}


def _sample(family, port):
    for sample in family.samples:
        if sample.labels.get("port") == str(port):
            return sample.value
    return None


def test_raw_parser_values_win_over_rounded_megabytes():
    reading = {
        "infos": _infos(),
        "raw": {
            "sum_rx": [3_634_567, 0, 0, 61_234, 1_060_999],
            "sum_tx": [1_442_101, 0, 0, 3_400_000, 3_500_000],
            "crc_errors": [0, 0, 0, 7, 0],
        },
        "ports": PORTS,
        "model": "GS305E",
        "duration": 1.2,
    }
    families = _collect(reading)

    # Exact byte counts, not the 10 kB-quantised 3.63 MB the library reports.
    assert _sample(families["netgear_plus_port_rx_bytes"], 1) == 3_634_567
    # CRC is present for every port, including ports the public dict omits.
    assert _sample(families["netgear_plus_port_crc_errors"], 4) == 7
    assert _sample(families["netgear_plus_port_crc_errors"], 1) == 0
    assert _sample(families["netgear_plus_raw_counters"], None) is None
    assert families["netgear_plus_raw_counters"].samples[0].value == 1.0


def test_falls_back_to_rescaled_megabytes_when_parser_unavailable():
    reading = {
        "infos": _infos(),
        "raw": None,
        "ports": PORTS,
        "model": "GS305E",
        "duration": 1.2,
    }
    families = _collect(reading)

    assert _sample(families["netgear_plus_port_rx_bytes"], 1) == 3_630_000
    assert families["netgear_plus_raw_counters"].samples[0].value == 0.0
    # No CRC is available on the fallback path; better absent than fabricated.
    assert families["netgear_plus_port_crc_errors"].samples == []


def test_link_state_and_speed_are_reported_per_port():
    reading = {
        "infos": _infos(),
        "raw": None,
        "ports": PORTS,
        "model": "GS305E",
        "duration": 1.0,
    }
    families = _collect(reading)

    assert _sample(families["netgear_plus_port_up"], 1) == 1.0
    assert _sample(families["netgear_plus_port_up"], 2) == 0.0
    assert _sample(families["netgear_plus_port_speed_mbps"], 4) == 100.0
    assert _sample(families["netgear_plus_port_speed_mbps"], 2) == 0.0


def test_description_is_a_label_on_its_own_info_metric():
    infos = _infos()
    infos["port_4_description"] = "doorbell-camera"
    reading = {"infos": infos, "raw": None, "ports": PORTS, "model": "GS305E", "duration": 1.0}
    families = _collect(reading)

    port_info = families["netgear_plus_port_info"]
    described = [s for s in port_info.samples if s.labels["port"] == "4"]
    assert described[0].labels["description"] == "doorbell-camera"
    # The numeric series must NOT carry the description, or renaming a port
    # orphans its history.
    speed_labels = families["netgear_plus_port_speed_mbps"].samples[0].labels
    assert "description" not in speed_labels


def test_failed_scrape_reports_down_instead_of_raising():
    families = _collect(RuntimeError("switch unreachable"))
    assert families["netgear_plus_up"].samples[0].value == 0.0
    assert "netgear_plus_port_up" not in families


def test_port_count_is_inferred_when_connector_does_not_report_it():
    reading = {"infos": _infos(), "raw": None, "ports": 0, "model": "", "duration": 1.0}
    families = _collect(reading)
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
    import py_netgear_plus

    assert hasattr(py_netgear_plus.NetgearSwitchConnector, "_get_port_statistics")
