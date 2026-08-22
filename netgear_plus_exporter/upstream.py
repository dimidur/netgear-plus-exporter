"""Everything that knows what py_netgear_plus looks like.

Changes when the library or the switch's firmware moves. Nothing here holds
state between polls, builds a metric, or decides how a problem is logged.

Counters come from the statistics the library parsed during its own poll and
retained: unrounded, cumulative and per port. ``get_switch_infos()`` reports
the same bytes rounded to megabytes and CRC as a per-interval delta for one
port, so it is used for everything except counters.

The library substitutes the previous poll's value for a byte counter that
parses negative on a connected port, so ``sum_rx`` and ``sum_tx`` are
post-repair while ``crc_errors`` is not. It logs ``Fallback to previous data``
at INFO when that fires.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import py_netgear_plus

from .errors import SwitchScrapeError

# Upstream's field names in the retained statistics.
_BYTE_KEYS = ("sum_rx", "sum_tx")
_COUNTER_KEYS = (*_BYTE_KEYS, "crc_errors")

# Why the counters could not be trusted. Names, not sentences: the caller owns
# the wording and the log level, so nothing here has to know how it is said.
INCOMPLETE_POLL = "incomplete-poll"
NO_RETAINED_DATA = "no-retained-data"
MISSING_LISTS = "missing-lists"
ROW_COUNT = "row-count"


@dataclass(frozen=True)
class Degraded:
    """One reason the raw counter path was refused, and what to say about it.

    ``detail`` is positional and its shape is fixed per reason, so the caller's
    message template and this tuple have to agree; only ROW_COUNT carries any.
    """

    reason: str
    detail: tuple[object, ...] = ()


class Connector(Protocol):
    """The ``py_netgear_plus.NetgearSwitchConnector`` methods this exporter calls.

    Unchecked against upstream, which ships no ``py.typed``. It buys typing on
    this side only: ``get_switch_infos()`` yields ``dict[str, Any]`` rather
    than ``Any``. Attributes are absent because every one is read with
    ``getattr``, behind guards that re-check the shape anyway.
    """

    def autodetect_model(self) -> Any: ...

    def get_login_cookie(self) -> bool: ...

    def get_switch_infos(self) -> dict[str, Any]: ...


def connect(host: str, password: str) -> Connector:
    """A logged-in connector, or SwitchScrapeError."""
    connector = py_netgear_plus.NetgearSwitchConnector(host, password)
    connector.autodetect_model()
    if not connector.get_login_cookie():
        msg = f"login rejected by {host}"
        raise SwitchScrapeError(msg)
    return connector


def poll_timestamp(connector: Connector) -> float:
    """The library's marker for when it last completed a full poll.

    Missing, renamed or unset, it reads as 0.0, which the freshness check
    treats as an unmoved timestamp.
    """
    return float(getattr(connector, "_previous_timestamp", 0.0) or 0.0)


def port_count(connector: Connector) -> int:
    return int(getattr(connector, "ports", 0) or 0)


def model_name(connector: Connector) -> str:
    return str(getattr(getattr(connector, "switch_model", None), "MODEL_NAME", ""))


def _bytes_went_all_zero(
    previous: dict[str, list[int]] | None, current: dict[str, list[int]]
) -> bool:
    """True when every rx/tx byte counter is zero but the previous reading
    had traffic somewhere. CRC is excluded from both sides.
    """
    if previous is None:
        return False
    had_traffic = any(any(previous.get(key) or []) for key in _BYTE_KEYS)
    all_zero_now = not any(any(current.get(key) or []) for key in _BYTE_KEYS)
    return had_traffic and all_zero_now


def read_statistics(
    connector: Connector,
    polled_before: float,
    previous_raw: dict[str, list[int]] | None,
    host: str,
) -> tuple[dict[str, list[int]] | None, Degraded | None]:
    """Unrounded per-port counters from the poll just taken, or a reason why not.

    Reads what the poll already parsed; it issues no request of its own.

    ``polled_before`` is the poll timestamp read before ``get_switch_infos()``.
    The library refreshes that timestamp and the parsed data together at the
    end of a successful poll, and returns before both when
    ``switch_model.SUPPORTED`` is false, so an unmoved timestamp means the
    retained data belongs to an older poll. No model sets that flag false at
    the pinned version.

    The parser pads short lists with zeros to exactly the port count, so a
    truncated statistics page arrives type-correct, length-correct and zeroed.
    Two checks catch that: a row count disagreeing with the port count is
    refused, and byte counters that all read zero immediately after a non-zero
    reading raise. Neither sees a break that yields negative counters, which
    the library repairs into a plateau first.
    """
    if poll_timestamp(connector) == polled_before:
        return None, Degraded(INCOMPLETE_POLL)
    # The parser output the poll retained, before any megabyte rounding.
    parsed = getattr(connector, "_previous_data", None)
    if not isinstance(parsed, dict) or not parsed:
        return None, Degraded(NO_RETAINED_DATA)
    if not all(isinstance(parsed.get(key), list) for key in _COUNTER_KEYS):
        return None, Degraded(MISSING_LISTS)
    ports = port_count(connector)
    if ports and any(len(parsed[key]) != ports for key in _COUNTER_KEYS):
        mismatched = next(key for key in _COUNTER_KEYS if len(parsed[key]) != ports)
        return None, Degraded(ROW_COUNT, (mismatched, len(parsed[mismatched]), ports))
    raw = {key: [int(value or 0) for value in parsed[key]] for key in _COUNTER_KEYS}
    if _bytes_went_all_zero(previous_raw, raw):
        msg = (
            f"all byte counters on {host} read zero right after a non-zero "
            "reading; a truncated statistics page is padded with zeros and "
            "looks exactly like this, so the scrape fails rather than "
            "exporting counters that may be a parse break (a real reboot or "
            "counter clear recovers automatically once rx/tx bytes move again)"
        )
        raise SwitchScrapeError(msg)
    return raw, None
