"""Tests for the device binding: what the library retains, and when to trust it.

Two kinds. Most drive `read_statistics` with stub connectors and no network.
A few read py-netgear-plus itself, including its source text, so that a
release which moves something this exporter depends on turns the suite red
rather than degrading the exporter silently. Those are the only tests here
that can fail without a change in this repo.
"""

from __future__ import annotations

import inspect
import logging

import py_netgear_plus
import pytest
from helpers import (
    PACKAGE_LOGGER,
    PORTS,
    RAW,
    SCRAPER_HOST,
    StubConnector,
    raw,
    read_statistics,
    scraper,
    zeros,
)

from netgear_plus_exporter import upstream
from netgear_plus_exporter.errors import SwitchScrapeError


def test_each_degraded_reason_warns_once_then_drops_to_debug(caplog):
    """A condition that degrades a scrape degrades every scrape. Unlatched,
    that is thousands of identical WARNING lines a day at a 30s interval.

    The latch is per reason, not per scraper: the third call takes a different
    branch and must warn, even though an earlier reason has already latched.
    """
    # Arrange -- one stub with no counter lists, one with too many rows.
    owner = scraper()
    no_lists = StubConnector({"something": "else"})
    extra_rows = StubConnector({key: [0] * (PORTS + 1) for key in RAW})

    # Act -- one reason twice, then a second reason twice.
    with caplog.at_level(logging.DEBUG, logger=PACKAGE_LOGGER):
        read_statistics(no_lists, owner=owner)
        read_statistics(no_lists, owner=owner)
        read_statistics(extra_rows, owner=owner)
        read_statistics(extra_rows, owner=owner)

    # Assert -- filtered by logger, since py_netgear_plus logs on its own.
    # The fourth call is what proves the second reason latched rather than
    # merely warned once.
    levels = [
        record.levelname for record in caplog.records if record.name.startswith(PACKAGE_LOGGER)
    ]
    assert levels == ["WARNING", "DEBUG", "WARNING", "DEBUG"]


def test_every_reason_has_something_to_say():
    """The reasons are names; the wording lives with the caller that logs
    them. A reason with no text renders as a KeyError at the moment it was
    needed, which is the one moment nothing else is going right.
    """
    # Arrange
    from netgear_plus_exporter.scraper import _DEGRADED_TEXT

    named = {
        upstream.INCOMPLETE_POLL,
        upstream.NO_RETAINED_DATA,
        upstream.MISSING_LISTS,
        upstream.ROW_COUNT,
    }

    # Act + Assert
    assert named == set(_DEGRADED_TEXT)


def test_an_empty_retained_dict_is_rejected_as_unavailable():
    """`_previous_data` is `{}` between `__init__` and `autodetect_model()`,
    and an empty dict passes an isinstance check.

    The exporter's own connect always autodetects, so this shape is not
    reachable through it today: it guards an upstream change that leaves the
    dict empty after a poll.

    Both this and the missing-lists branch refuse the counters, so asserting
    only "None and something was logged" cannot tell them apart and passes
    even with the emptiness check removed. The reason is what is pinned: an
    empty dict means the path is unavailable, not that the payload arrived
    malformed.
    """
    # Arrange -- a connector whose poll never completed.
    never_polled = StubConnector({})

    # Act
    counters, degraded = upstream.read_statistics(never_polled, 0.0, None, SCRAPER_HOST)

    # Assert
    assert counters is None
    assert degraded == upstream.Degraded(upstream.NO_RETAINED_DATA)


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
    counters, degraded = upstream.read_statistics(StubConnector(parsed), 0.0, None, SCRAPER_HOST)

    # Assert -- the detail is what the message interpolates, so it is pinned.
    assert counters is None
    assert degraded == upstream.Degraded(upstream.ROW_COUNT, ("sum_rx", 6, PORTS))


def test_an_incomplete_poll_is_refused():
    """An unmoved timestamp means the retained data belongs to an older poll."""
    # Arrange -- the stub's timestamp matches what the caller read before.
    connector = StubConnector(raw(), timestamp=7.0)

    # Act
    counters, degraded = upstream.read_statistics(connector, 7.0, None, SCRAPER_HOST)

    # Assert
    assert counters is None
    assert degraded == upstream.Degraded(upstream.INCOMPLETE_POLL)


def test_correct_length_statistics_pass_the_guard():
    # Arrange
    parsed = raw()

    # Act
    counters = read_statistics(StubConnector(parsed))

    # Assert
    assert counters == RAW


def test_length_guard_is_skipped_when_the_port_count_is_unknown():
    """ports=0 means the connector never learned the count; rejecting on a
    comparison against 0 would reject every reading.
    """
    # Arrange
    parsed = raw()

    # Act
    counters = read_statistics(StubConnector(parsed, ports=0))

    # Assert
    assert counters == RAW


def test_all_zero_bytes_after_traffic_fail_the_scrape():
    """A truncated statistics page arrives type-correct, length-correct and
    zero-padded -- indistinguishable from a healthy idle switch except for
    history. Counters do not all reset while traffic was flowing, so the
    scrape fails loudly instead of exporting flat zeros with up=1.

    One of the SwitchScrapeErrors raised outside the reconnect path, so its
    message names the switch like the others in that set.
    """
    # Arrange
    connector = StubConnector(zeros())

    # Act + Assert
    with pytest.raises(SwitchScrapeError, match="parse break") as caught:
        read_statistics(connector, RAW)

    assert SCRAPER_HOST in str(caught.value)


def test_all_zero_bytes_with_no_history_are_a_valid_reading():
    # Arrange -- a brand-new idle switch really does read all zeros.
    empty = zeros()

    # Act
    counters = read_statistics(StubConnector(empty))

    # Assert
    assert counters == empty


def test_zero_transition_ignores_crc_history():
    """CRC-only history must not arm the trap: all-zero CRC is the healthy
    steady state, so it proves nothing about whether bytes were flowing.
    """
    # Arrange
    crc_only_history = dict(zeros(), crc_errors=[3, 0, 0, 0, 0])
    crc_only_now = dict(zeros(), crc_errors=[9, 0, 0, 0, 0])

    # Act + Assert -- CRC on the history side proves nothing...
    assert upstream._bytes_went_all_zero(crc_only_history, zeros()) is False
    assert upstream._bytes_went_all_zero(RAW, zeros()) is True
    assert upstream._bytes_went_all_zero(zeros(), zeros()) is False
    assert upstream._bytes_went_all_zero(None, zeros()) is False
    # ...and CRC on the current side buys no amnesty: a partially truncated
    # page can keep a nonzero CRC cell while every byte counter zeroes out,
    # which is exactly the parse break this exists to catch.
    assert upstream._bytes_went_all_zero(RAW, crc_only_now) is True


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
    connector = py_netgear_plus.NetgearSwitchConnector(SCRAPER_HOST, "unused")

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

    `read_statistics` treats an unmoved `_previous_timestamp` as proof that
    the retained data is from an earlier poll. That holds only while upstream
    assigns the timestamp and the data together, after the early return for an
    unsupported model. Hoisting the timestamp above that return is a plausible
    refactor, since upstream reads it only for `sample_time`, and it would
    turn the guard into a no-op that exports a zero-filled reseed as live data.

    Reads source text, so it is brittle by design: a bump that reformats this
    method should fail here and be re-verified by hand.
    """
    # Arrange
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


def test_upstream_still_raises_model_detection_with_no_arguments():
    """The premise under the %r rendering: this class carries no message, so
    a release that gave it one would make those tests pass for free.
    """
    # Arrange
    from py_netgear_plus.models import SwitchModelNotDetectedError

    # Act + Assert
    assert str(SwitchModelNotDetectedError()) == ""


def test_upstream_repairs_negative_bytes_and_leaves_crc_alone():
    """Counters come from the data the library retains, which is post-repair.
    This pins the two halves of that contract, in memory and without hardware,
    because a release that started writing back into the crc_errors list would
    corrupt CRC with every gate still green.
    """
    # Arrange
    connector = py_netgear_plus.NetgearSwitchConnector(SCRAPER_HOST, "unused")
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
