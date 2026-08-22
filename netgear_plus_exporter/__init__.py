"""Prometheus exporter for NETGEAR Plus / Easy Smart Managed switches.

These switches expose no SNMP, so everything comes from scraping the web UI.
One reason to change per module: :mod:`upstream` knows what py_netgear_plus
and the switch look like, :mod:`scraper` owns the session and decides when the
switch is actually polled, :mod:`collector` turns a reading into metrics.
:mod:`errors` holds the exception they share and :mod:`__main__` wires them
together.
"""

from .collector import NetgearSwitchCollector
from .errors import SwitchScrapeError
from .scraper import NetgearSwitchScraper

__version__ = "0.2.0"
__all__ = [
    "NetgearSwitchCollector",
    "NetgearSwitchScraper",
    "SwitchScrapeError",
    "__version__",
]
