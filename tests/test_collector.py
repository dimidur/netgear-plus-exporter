"""Tests for turning one reading into metrics.

The collector is fed canned readings, so nothing here needs a switch or even
a scraper. What is pinned: which counter derivation wins, the latch that stops
the exporter switching between them mid-series, the fallback's deliberate
omissions, and what reaches /metrics and the log when a scrape fails.
"""

from __future__ import annotations

import logging

import pytest
from helpers import (
    PACKAGE_LOGGER,
    PORTS,
    RAW,
    STUB_HOST,
    STUB_SWITCH,
    collect,
    collector,
    families,
    infos,
    raw,
    reading,
    record_starting,
    records_starting,
    sample,
)
from prometheus_client import CollectorRegistry, generate_latest
from py_netgear_plus.models import SwitchModelNotDetectedError

from netgear_plus_exporter.errors import SwitchScrapeError


def test_raw_parser_values_win_over_rounded_megabytes():
    # Arrange + Act
    found = collect(reading(raw=RAW))

    # Assert -- exact byte counts, not the 10 kB-quantised 3.63 MB the library reports.
    assert sample(found["netgear_plus_port_rx_bytes"], 1) == 3_634_567
    # CRC is present for every port, including ports the public dict omits.
    assert sample(found["netgear_plus_port_crc_errors"], 4) == 7
    assert sample(found["netgear_plus_port_crc_errors"], 1) == 0
    # Port 5 discriminates the two sources: the parser says 0, the public
    # dict's delta says 3. The raw path must report the parser.
    assert sample(found["netgear_plus_port_crc_errors"], 5) == 0
    # Switch-scoped, not per-port; sample() matches on a "port" label.
    assert "port" not in found["netgear_plus_raw_counters"].samples[0].labels
    assert found["netgear_plus_raw_counters"].samples[0].value == 1.0


def test_sample_interval_is_named_and_described_for_what_it_measures():
    """response_time_s is the gap between polls, so neither half of the old
    name held: it is not switch-reported and it is not a response time.

    The name alone is not enough -- a HELP string can reintroduce the same
    false claim under a correct name -- so the description is pinned too.
    """
    # Arrange + Act
    found = collect(reading())

    # Assert
    assert "netgear_plus_switch_response_seconds" not in found
    interval = found["netgear_plus_library_sample_interval_seconds"]
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
    without = infos()
    del without["response_time_s"]

    # Act
    found = collect(reading(infos=without))

    # Assert
    assert "netgear_plus_library_sample_interval_seconds" not in found


def test_sample_interval_of_zero_is_a_real_sample():
    """Zero is a value, not an absence, so the guard tests `is not None`.

    Nothing in normal operation produces it -- the library sleeps 0.25s twice
    per poll, putting a 0.5s floor under the interval -- so this is purely
    defensive. A truthiness check would drop the sample instead of exporting it.
    """
    # Arrange + Act
    found = collect(reading(infos=infos(response_time_s=0)))

    # Assert
    assert found["netgear_plus_library_sample_interval_seconds"].samples[0].value == 0.0


def test_falls_back_to_rescaled_megabytes_when_parser_unavailable():
    # Arrange + Act
    found = collect(reading())

    # Assert
    assert sample(found["netgear_plus_port_rx_bytes"], 1) == 3_630_000
    assert found["netgear_plus_raw_counters"].samples[0].value == 0.0
    # The fixture carries port_5_crc_errors, so this fails if it is exported.
    assert found["netgear_plus_port_crc_errors"].samples == []


def test_fallback_never_exports_the_per_interval_crc_delta():
    """The key is a delta, so it stays out even if upstream ever fixes the
    one-port-only bug and populates every port.
    """
    # Arrange
    populated = infos()
    for port in range(1, PORTS + 1):
        populated[f"port_{port}_crc_errors"] = 999
    populated["sum_port_crc_errors"] = 999 * PORTS

    # Act
    found = collect(reading(infos=populated))

    # Assert
    crc = found["netgear_plus_port_crc_errors"]
    assert crc.samples == []
    # The empty family is all a /metrics reader sees, so the HELP has to say
    # the absence is deliberate.
    assert "Absent when netgear_plus_raw_counters is 0." in crc.documentation


def test_link_state_and_speed_are_reported_per_port():
    # Arrange + Act
    found = collect(reading())

    # Assert
    assert sample(found["netgear_plus_port_up"], 1) == 1.0
    assert sample(found["netgear_plus_port_up"], 2) == 0.0
    assert sample(found["netgear_plus_port_speed_mbps"], 4) == 100.0
    assert sample(found["netgear_plus_port_speed_mbps"], 2) == 0.0


def test_description_is_a_label_on_its_own_info_metric():
    # Arrange
    named = infos()
    named["port_4_description"] = "doorbell-camera"

    # Act
    found = collect(reading(infos=named))

    # Assert
    port_info = found["netgear_plus_port_info"]
    described = [entry for entry in port_info.samples if entry.labels["port"] == "4"]
    assert described[0].labels["description"] == "doorbell-camera"
    # The numeric series must NOT carry the description, or renaming a port
    # orphans its history.
    assert "description" not in found["netgear_plus_port_speed_mbps"].samples[0].labels


def test_port_count_is_inferred_when_the_reading_does_not_report_it():
    # Arrange + Act
    found = collect(reading(ports=0, model=""))

    # Assert
    assert len(found["netgear_plus_port_up"].samples) == PORTS


def test_losing_the_raw_path_fails_the_scrape_instead_of_switching():
    """Losing the raw path fails the scrape rather than switching derivations
    -- see the latch note in README.md for why a switch corrupts the series.
    """
    # Arrange
    instance = collector(
        reading(raw=RAW),
        reading(),  # raw lost: parser layer stopped answering
        reading(raw=RAW),  # and back
    )

    # Act + Assert -- three rounds; each collect is one act on the latch.
    first = families(instance)
    assert first["netgear_plus_up"].samples[0].value == 1.0
    assert sample(first["netgear_plus_port_rx_bytes"], 1) == 3_634_567

    # The flip is refused: down, and no counters -- NOT 3_630_000 from the
    # fallback derivation into the same series.
    lost = families(instance)
    assert lost["netgear_plus_up"].samples[0].value == 0.0
    assert "netgear_plus_port_rx_bytes" not in lost

    # A refusal, not a poisoning: the raw path coming back resumes normally.
    recovered = families(instance)
    assert recovered["netgear_plus_up"].samples[0].value == 1.0
    assert sample(recovered["netgear_plus_port_rx_bytes"], 1) == 3_634_567


def test_the_lost_raw_path_error_names_the_switch():
    """The one SwitchScrapeError raised inside the collector.

    Asserted on the exception, not the log line: the line names the host in
    its prefix too, so a log assertion passes with the message saying nothing
    about which switch lost the path.
    """
    # Arrange -- latch the raw path, so losing it is a refusal.
    instance = collector(reading(raw=RAW))
    families(instance)

    # Act + Assert
    with pytest.raises(SwitchScrapeError, match="raw counter path") as caught:
        instance._effective_raw(None)

    assert STUB_HOST in str(caught.value)


def test_fallback_stays_latched_when_raw_appears():
    """The upgrade direction is deferred to a restart: jumping to raw
    mid-series is the same discontinuity as the other direction, mirrored.
    """
    # Arrange
    instance = collector(reading(), reading(raw=RAW))

    # Act + Assert -- two rounds; the second is where an upgrade could flip.
    first = families(instance)
    assert sample(first["netgear_plus_port_rx_bytes"], 1) == 3_630_000

    upgraded = families(instance)
    assert upgraded["netgear_plus_up"].samples[0].value == 1.0
    assert sample(upgraded["netgear_plus_port_rx_bytes"], 1) == 3_630_000
    # raw_counters reports the path the EXPORTED counters use, which is still
    # the fallback -- not what the reading happened to carry.
    assert upgraded["netgear_plus_raw_counters"].samples[0].value == 0.0


def test_upgrade_notice_is_logged_exactly_once(caplog):
    """While fallback is latched and raw is available, the exporter logs that
    a restart would upgrade -- once. Unguarded, the notice would repeat on
    every collect: ~2,900 lines per day at a 30s scrape interval, forever.
    """
    # Arrange
    instance = collector(reading(), reading(raw=RAW))

    # Act -- three rounds in the upgrade condition.
    with caplog.at_level(logging.INFO, logger=PACKAGE_LOGGER):
        for _ in range(3):
            families(instance)

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
    instance = collector(RuntimeError("switch unreachable"), reading(raw=RAW))

    # Act + Assert -- the failure must not choose a path for round two.
    down = families(instance)
    assert down["netgear_plus_up"].samples[0].value == 0.0

    first_good = families(instance)
    assert first_good["netgear_plus_raw_counters"].samples[0].value == 1.0
    assert sample(first_good["netgear_plus_port_rx_bytes"], 1) == 3_634_567


def test_failed_scrape_reports_down_instead_of_raising():
    # Arrange + Act
    found = collect(RuntimeError("switch unreachable"))

    # Assert
    assert found["netgear_plus_up"].samples[0].value == 0.0
    assert "netgear_plus_port_up" not in found


def test_metric_construction_failure_still_reports_down(monkeypatch):
    """A failure while building metric families must reach /metrics as up 0.

    Asserted through a real registry, because the failure this pins only
    exists there: a generator's body runs during registry iteration, after
    collect() has returned, so an exception raised in one reaches
    generate_latest as HTTP 500 and takes the already-yielded up sample with
    it. Iterating collect() directly cannot tell the two arrangements apart.
    """
    # Arrange
    instance = collector(reading(raw=raw()))

    def explode(value, switch, counters):
        yield from ()
        msg = "metric construction failed"
        raise RuntimeError(msg)

    monkeypatch.setattr(instance, "_ports", explode)
    registry = CollectorRegistry()
    registry.register(instance)

    # Act
    exposition = generate_latest(registry)

    # Assert
    assert b'netgear_plus_up{switch="test-switch"} 0.0' in exposition
    assert b"netgear_plus_port_up" not in exposition
    # _describe built successfully and is discarded with the rest: a partial
    # exposition would be worse than none, since the families that did build
    # would look like a complete reading.
    assert b"netgear_plus_scrape_duration_seconds" not in exposition


def test_an_unusable_duration_fails_the_scrape_rather_than_the_endpoint():
    """generate_latest calls float() on every sample value, and it does that
    after collect() has returned. Casting the duration where the family is
    built moves that conversion inside collect()'s try, so a reading the
    scraper should never have produced comes out as up 0 instead of as a 500
    that reports nothing at all.
    """
    # Arrange
    instance = collector(reading(raw=raw(), duration="not-a-float"))
    registry = CollectorRegistry()
    registry.register(instance)

    # Act
    exposition = generate_latest(registry)

    # Assert
    assert b'netgear_plus_up{switch="test-switch"} 0.0' in exposition
    assert b"netgear_plus_scrape_duration_seconds{" not in exposition


def test_an_exception_with_no_message_still_names_itself(caplog):
    """A library exception can reach the collector unwrapped, with nothing on
    the line to identify it. Rendered with %s that line ends at the colon.
    """
    # Arrange
    caplog.set_level(logging.DEBUG, logger=PACKAGE_LOGGER)

    # Act
    found = collect(SwitchModelNotDetectedError())

    # Assert
    assert found["netgear_plus_up"].samples[0].value == 0.0
    logged = record_starting(caplog, "scrape of")
    assert "SwitchModelNotDetectedError" in logged.getMessage()
    assert not logged.getMessage().endswith(": ")
    # This exception names nothing of its own, so the line's identifiers are
    # the only ones: the metric label, and the address that failed.
    assert STUB_SWITCH in logged.getMessage()
    assert STUB_HOST in logged.getMessage()


def test_a_repeating_failure_logs_one_traceback(caplog):
    """A switch that is down stays down and every scrape logs it. The WARNING
    keeps going out; the traceback does not repeat, because thousands a day
    for one fault buries everything else. It comes back when the failure
    changes or a scrape succeeds.
    """
    # Arrange
    caplog.set_level(logging.DEBUG, logger=PACKAGE_LOGGER)
    unreachable = OSError("no route to host")
    instance = collector(unreachable, unreachable, ValueError("something else"))

    # Act
    for _ in range(3):
        families(instance)

    # Assert
    # bool(), not `is not None`: logging stores a falsy exc_info as False.
    assert [bool(record.exc_info) for record in records_starting(caplog, "scrape of")] == [
        True,
        False,
        True,
    ]


def test_two_faults_of_the_exporters_own_type_are_told_apart(caplog):
    """Every fault the exporter raises itself is a SwitchScrapeError, so a key
    of the type alone would treat an empty response and a rejected login as
    one fault and swallow the second traceback.
    """
    # Arrange
    caplog.set_level(logging.DEBUG, logger=PACKAGE_LOGGER)
    instance = collector(
        SwitchScrapeError("empty response from 192.0.2.1"),
        SwitchScrapeError("login rejected by 192.0.2.1"),
    )

    # Act
    for _ in range(2):
        families(instance)

    # Assert
    assert [bool(record.exc_info) for record in records_starting(caplog, "scrape of")] == [
        True,
        True,
    ]


def test_a_recovery_rearms_the_traceback(caplog):
    """The suppression is per consecutive run, not per process: the same
    failure after a good scrape is news again.
    """
    # Arrange
    caplog.set_level(logging.DEBUG, logger=PACKAGE_LOGGER)
    unreachable = OSError("no route to host")
    instance = collector(unreachable, reading(), unreachable)

    # Act
    for _ in range(3):
        families(instance)

    # Assert
    assert [bool(record.exc_info) for record in records_starting(caplog, "scrape of")] == [
        True,
        True,
    ]
