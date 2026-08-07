"""Prometheus exporter for NETGEAR Plus / Easy Smart Managed switches."""

from .exporter import (
    NetgearSwitchCollector,
    NetgearSwitchScraper,
    SwitchScrapeError,
)

__version__ = "0.2.0"
__all__ = [
    "NetgearSwitchCollector",
    "NetgearSwitchScraper",
    "SwitchScrapeError",
    "__version__",
]
