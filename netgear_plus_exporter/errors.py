"""The exporter's own failure type."""

from __future__ import annotations


class SwitchScrapeError(Exception):
    """Raised when a scrape cycle fails.

    Messages name the switch, and carry the ``repr`` of any failure they
    wrap: several py_netgear_plus exceptions are raised with no message.

    Nothing catches this by type. The collector's handler is blind on purpose,
    so a dead switch cannot 500 the endpoint; the type is for the traceback.
    """
