"""Tests for the session and how often the switch is actually polled.

Stub connectors throughout, so no network is involved. What is pinned: one
poll per scrape, what a failed or contended scrape serves, what survives in
the cache across a failure, and what reaches the log on the way past.
"""

from __future__ import annotations

import logging

import pytest
from helpers import (
    PACKAGE_LOGGER,
    RAW,
    SCRAPER_HOST,
    StubPollingConnector,
    raw,
    record_starting,
    records_starting,
    scraper_failing_with,
    zeros,
)
from py_netgear_plus.models import SwitchModelNotDetectedError

from netgear_plus_exporter import scraper as scraper_module
from netgear_plus_exporter import upstream
from netgear_plus_exporter.errors import SwitchScrapeError
from netgear_plus_exporter.scraper import NetgearSwitchScraper


def _scraper_with(connector, **kwargs) -> NetgearSwitchScraper:
    instance = NetgearSwitchScraper(SCRAPER_HOST, "unused", cache_seconds=0.0, **kwargs)
    instance._connector = connector
    return instance


def test_one_scrape_polls_the_switch_once():
    """One poll per scrape, not two. See docs/own-the-protocol.md for why a
    duplicate fetch is a real cost on this hardware.

    The stub has no _get_port_statistics, so a second fetch raises rather
    than passing quietly.
    """
    # Arrange
    connector = StubPollingConnector([raw()])
    instance = _scraper_with(connector)

    # Act
    reading = instance.scrape()

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
    connector = StubPollingConnector([raw()], completes=False)
    connector._previous_data = raw()  # left over from an earlier poll
    instance = _scraper_with(connector)

    # Act
    reading = instance.scrape()

    # Assert
    assert reading["raw"] is None


def test_reconnect_reads_the_timestamp_from_the_replacement_connector(monkeypatch):
    """time.perf_counter() is process-wide, so a timestamp taken from the
    connector that failed is still comparable against the one that replaced
    it, and that is the trap: a fresh connector's __init__ stamp is always
    greater than the old connector's, so an early-returning poll on the new
    one would pass the guard and publish its zero-seeded retained data as a
    live reading.

    Deleting the reassignment in _read() turns this red.
    """

    # Arrange -- first connector fails, replacement never completes a poll.
    # Not scraper_failing_with: this needs a specific replacement connector,
    # seeded with the timestamp and retained data the guard has to reject.
    class _PollRaisesWithStamp(StubPollingConnector):
        def get_switch_infos(self):
            raise RuntimeError("poll failed")

    failing = _PollRaisesWithStamp([raw()])
    failing._previous_timestamp = 100.0
    instance = _scraper_with(failing)

    replacement = StubPollingConnector([raw()], completes=False)
    replacement._previous_timestamp = 500.0  # a later __init__ stamp
    # Zero-filled lists are what _set_instance_attributes_by_model seeds,
    # reached via autodetect_model() during connect. __init__ alone leaves
    # _previous_data an empty dict.
    replacement._previous_data = zeros()
    monkeypatch.setattr(instance, "_connect", lambda: replacement)

    # Act
    reading = instance.scrape()

    # Assert -- the incomplete poll is not reported as a reading.
    assert reading["raw"] is None


def test_a_recovered_poll_is_logged_with_its_exception(caplog, monkeypatch):
    """When the retry succeeds, this line is the only record that anything
    went wrong at all.

    Logged at INFO without the exception, every cause reads as a routine
    session expiry, which is the one thing it cannot be: the library refreshes
    an expired cookie in place, so a parse break and an unplugged switch
    arrive here looking identical.
    """
    # Arrange
    instance = scraper_failing_with(ValueError("port status page did not parse"), monkeypatch)
    # Captured from DEBUG up, so a demotion to INFO reaches the level assertion
    # below instead of vanishing from caplog.records.
    caplog.set_level(logging.DEBUG, logger=PACKAGE_LOGGER)

    # Act
    reading = instance.scrape()

    # Assert
    record = record_starting(caplog, "poll of")
    assert record.levelno == logging.WARNING
    assert record.exc_info is not None
    assert "port status page did not parse" in caplog.text
    # The reconnect still has to produce a reading.
    assert reading["infos"]


def test_a_recurring_recovered_poll_logs_one_traceback(caplog, monkeypatch):
    """A switch that fails one poll per cycle and recovers on the retry is a
    real fault, but a traceback every cache interval buries everything else.
    The WARNING keeps going out.
    """

    # Arrange -- fails the first poll of every scrape, succeeds on the retry.
    class _Flaky(StubPollingConnector):
        calls = 0

        def get_switch_infos(self):
            self.calls += 1
            if self.calls % 2:
                msg = "port status page did not parse"
                raise ValueError(msg)
            return super().get_switch_infos()

    flaky = _Flaky([raw()])
    instance = _scraper_with(flaky)
    monkeypatch.setattr(instance, "_connect", lambda: flaky)
    caplog.set_level(logging.DEBUG, logger=PACKAGE_LOGGER)

    # Act
    for _ in range(3):
        instance.scrape()

    # Assert
    recovered = records_starting(caplog, "poll of")
    assert len(recovered) == 3
    assert [bool(record.exc_info) for record in recovered] == [True, False, False]


def test_a_failed_reconnect_logs_nothing_of_its_own(caplog, monkeypatch):
    """Nothing is lost by staying quiet: the wrapper raised below carries the
    original as __context__, and the collector logs the whole chain once,
    latched. Logging here as well doubles the traceback volume for one fault.
    """
    # Arrange
    instance = scraper_failing_with(
        ValueError("port status page did not parse"), monkeypatch, OSError("no route to host")
    )
    caplog.set_level(logging.DEBUG, logger=PACKAGE_LOGGER)

    # Act
    with pytest.raises(SwitchScrapeError) as caught:
        instance.scrape()

    # Assert
    assert records_starting(caplog, "poll of") == []
    assert isinstance(caught.value.__cause__.__context__, ValueError)


def test_a_failed_reconnect_raises_the_project_error_type(monkeypatch):
    """SwitchScrapeError marks a scrape failure as expected rather than a
    programming error. Nothing catches it by type: the collector's handler is
    deliberately blind, because a dead switch must not 500 the endpoint. The
    value is in the traceback, so both causes have to survive the wrap.

    The message has to stand alone: it surfaces in tracebacks and to any
    future caller, so one naming neither the switch nor the failure it wrapped
    says nothing about which switch did what.
    """
    # Arrange
    instance = scraper_failing_with(
        RuntimeError("poll failed"), monkeypatch, OSError("switch unreachable")
    )

    # Act + Assert
    with pytest.raises(SwitchScrapeError) as caught:
        instance.scrape()

    assert SCRAPER_HOST in str(caught.value)
    assert "switch unreachable" in str(caught.value)

    assert isinstance(caught.value.__cause__, OSError)
    assert "switch unreachable" in str(caught.value.__cause__)
    # The poll that started it survives one link further down the chain, which
    # is what makes the collector's exc_info worth logging.
    assert isinstance(caught.value.__cause__.__context__, RuntimeError)
    assert "poll failed" in str(caught.value.__cause__.__context__)


def test_a_wrapped_exception_with_no_message_is_named_by_class(monkeypatch):
    """The wrap renders a repr, not a str.

    Upstream raises PageFetcherConnectionError, NotLoggedInError and
    SwitchModelNotDetectedError with no arguments, and all three can be the
    reconnect failure. Rendered as text they contribute nothing and the
    message ends in a dangling colon.
    """
    # Arrange
    instance = scraper_failing_with(
        RuntimeError("poll failed"), monkeypatch, SwitchModelNotDetectedError()
    )

    # Act + Assert
    with pytest.raises(SwitchScrapeError) as caught:
        instance.scrape()

    assert "SwitchModelNotDetectedError" in str(caught.value)
    assert not str(caught.value).endswith(": ")


def test_an_empty_response_names_the_switch():
    """Every SwitchScrapeError names the switch. One of the set raised outside
    the reconnect path: an empty response, a rejected login, and the
    all-zero-bytes guard.

    Matched on the message as well as the type, so a regression that routed
    this into the reconnect path, whose wrapper also names the host, does not
    pass for the wrong reason.
    """

    # Arrange
    class _EmptyConnector(StubPollingConnector):
        def get_switch_infos(self):
            return {}

    instance = _scraper_with(_EmptyConnector([raw()]))

    # Act + Assert
    with pytest.raises(SwitchScrapeError, match="empty response") as caught:
        instance.scrape()

    assert SCRAPER_HOST in str(caught.value)


def test_a_rejected_login_names_the_switch(monkeypatch):
    """Of the same set, raised by connect before any poll runs."""

    # Arrange
    class _RejectingConnector:
        def __init__(self, host, password):
            pass

        def autodetect_model(self):
            return None

        def get_login_cookie(self):
            return False

    monkeypatch.setattr(upstream.py_netgear_plus, "NetgearSwitchConnector", _RejectingConnector)
    instance = NetgearSwitchScraper("192.0.2.2", "unused", cache_seconds=0.0)

    # Act + Assert
    with pytest.raises(SwitchScrapeError, match="login rejected") as caught:
        instance._connect()

    assert "192.0.2.2" in str(caught.value)


def test_scrape_wires_counter_history_and_preserves_it_on_failure():
    """The zero-transition guard only works if the poll hands it the previous
    successful counters AND a failed scrape leaves those untouched. Recording
    the zeroed reading as history would silently break recovery: the next
    zeros would then pass.
    """
    # Arrange -- connection pre-seeded, so no network is involved.
    instance = _scraper_with(StubPollingConnector([raw(), zeros(), zeros()]))

    # Act + Assert -- traffic, then zeros, then zeros again.
    assert instance.scrape()["raw"] == RAW
    with pytest.raises(SwitchScrapeError, match="parse break"):
        instance.scrape()
    # History survived the failed scrape: still compared against RAW.
    with pytest.raises(SwitchScrapeError, match="parse break"):
        instance.scrape()


def test_a_poll_without_counters_keeps_the_counter_history():
    """A poll can succeed with no counters: any of the raw path's guards
    refuses them while the rest of the reading is fine.

    Dropping the history there disarms the zero-transition guard, and the next
    zero-padded statistics page is exported as a genuine counter reset with
    netgear_plus_up 1. Two polls reach that state.
    """
    # Arrange -- traffic, then a poll whose retained data never refreshed.
    instance = _scraper_with(StubPollingConnector([raw()]))
    assert instance.scrape()["raw"] == RAW

    stalled = StubPollingConnector([raw()], completes=False)
    stalled._previous_data = raw()
    instance._connector = stalled

    # Act
    assert instance.scrape()["raw"] is None

    # Assert -- history survived, so an all-zero page still fails the scrape.
    assert instance._history == RAW
    instance._connector = StubPollingConnector([zeros()])
    with pytest.raises(SwitchScrapeError, match="parse break"):
        instance.scrape()


def test_a_failure_is_cached_like_a_success(monkeypatch):
    """A dead switch costs one poll per cache interval, not one per scrape.

    Without this every collect re-attacks a switch already known to be down,
    each attempt running the connect timeouts in full while holding the lock.
    """
    # Arrange
    instance = scraper_failing_with(
        RuntimeError("poll failed"), monkeypatch, OSError("no route to host")
    )
    instance.cache_seconds = 300.0
    attempts = []
    failing_connect = instance._connect

    def _counting_connect():
        attempts.append(1)
        return failing_connect()

    monkeypatch.setattr(instance, "_connect", _counting_connect)

    # Act -- three scrapes inside one cache interval.
    for _ in range(3):
        with pytest.raises(SwitchScrapeError):
            instance.scrape()

    # Assert
    assert len(attempts) == 1


def test_a_replayed_failure_keeps_the_original_cause(monkeypatch):
    """The replay raises a new exception rather than the stored one, because
    re-raising a single object appends a frame to its traceback every time.
    The cause still has to reach the log.
    """
    # Arrange
    instance = scraper_failing_with(
        RuntimeError("poll failed"), monkeypatch, OSError("no route to host")
    )
    instance.cache_seconds = 300.0
    with pytest.raises(SwitchScrapeError) as first:
        instance.scrape()

    # Act
    with pytest.raises(SwitchScrapeError, match="failed its last poll") as replayed:
        instance.scrape()

    # Assert
    assert replayed.value is not first.value
    assert "no route to host" in str(replayed.value.__cause__)
    assert SCRAPER_HOST in str(replayed.value)


def test_a_switch_that_dies_stops_serving_its_last_good_reading(monkeypatch):
    """The state a naive cache gets wrong: a reading exists AND the last poll
    failed. Serving the reading publishes netgear_plus_up 1 for a switch that
    is down, for the whole cache interval.

    The reading itself is still needed, as the counter history the
    zero-transition guard compares against, which is why the two are separate
    fields rather than one that has to be arbitrated.
    """
    # Arrange -- one good poll, then the switch stops answering.
    instance = _scraper_with(StubPollingConnector([raw()]))
    assert instance.scrape()["raw"] == RAW

    class _Unreachable(StubPollingConnector):
        def get_switch_infos(self):
            raise OSError("no route to host")

    instance._connector = _Unreachable([raw()])
    monkeypatch.setattr(instance, "_connect", lambda: instance._connector)
    with pytest.raises(SwitchScrapeError):
        instance.scrape()

    # Act + Assert -- fresh replay, within the interval. Matched, so a
    # regression that recorded no outcome at all takes the cold-start branch
    # and fails this rather than passing on the same exception type.
    instance.cache_seconds = 300.0
    with pytest.raises(SwitchScrapeError, match="failed its last poll"):
        instance.scrape()

    # ...and under contention, which takes the other path into _replay.
    instance._lock.acquire()
    try:
        with pytest.raises(SwitchScrapeError, match="failed its last poll"):
            instance.scrape()
    finally:
        instance._lock.release()

    # The counter history survived both, so recovery still has something to
    # compare a zeroed page against.
    assert instance._history == RAW


def test_a_success_clears_the_cached_failure(monkeypatch):
    """A switch that comes back must stop replaying the failure it had.

    The recorded failure is what _replay serves, so leaving it in place after
    a good poll pins the exporter to a fault the switch has recovered from,
    for as long as anything reaches _replay.
    """
    # Arrange -- one failed poll, then the switch answers again.
    instance = scraper_failing_with(
        RuntimeError("poll failed"), monkeypatch, OSError("no route to host")
    )
    with pytest.raises(SwitchScrapeError):
        instance.scrape()
    instance._connector = StubPollingConnector([raw()])

    # Act
    recovered = instance.scrape()

    # Assert -- and a caller that cannot take the lock gets that, not the
    # failure from before.
    assert recovered["raw"] == RAW
    instance._lock.acquire()
    try:
        assert instance.scrape() == recovered
    finally:
        instance._lock.release()


def test_a_first_scrape_polls_even_seconds_after_boot(monkeypatch):
    """time.monotonic() is uptime on Linux, so a process started in the first
    seconds after boot reads single digits. Against an initial timestamp of
    0.0 that looks like an attempt made moments ago, and the first scrape
    replays a reading that does not exist instead of taking one.
    """
    # Arrange -- five seconds of uptime, a thirty second cache.
    monkeypatch.setattr(scraper_module.time, "monotonic", lambda: 5.0)
    instance = NetgearSwitchScraper(SCRAPER_HOST, "unused", cache_seconds=30.0)
    instance._connector = StubPollingConnector([raw()])

    # Act
    reading = instance.scrape()

    # Assert
    assert reading["raw"] == RAW


def test_a_contended_scrape_serves_the_cache_instead_of_queueing():
    """A poll against an unreachable switch can hold the lock for longer than
    a Prometheus scrape timeout. Waiting for it loses the up sample that says
    the switch is down, so a caller that cannot take the lock replays.
    """
    # Arrange -- a reading is cached, then the lock is held by "another thread".
    instance = _scraper_with(StubPollingConnector([raw()]))
    first = instance.scrape()
    instance._lock.acquire()

    # Act
    served = instance.scrape()

    # Assert
    instance._lock.release()
    assert served == first


def test_a_contended_scrape_with_no_reading_yet_fails():
    """Cold start under contention: there is nothing to replay, and a reading
    that does not exist must not be invented.
    """
    # Arrange
    instance = NetgearSwitchScraper(SCRAPER_HOST, "unused", cache_seconds=0.0)
    instance._lock.acquire()

    # Act + Assert
    with pytest.raises(SwitchScrapeError, match="no reading"):
        instance.scrape()

    instance._lock.release()
