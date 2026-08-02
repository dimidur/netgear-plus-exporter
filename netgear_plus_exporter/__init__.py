"""Prometheus exporter for NETGEAR Plus / Easy Smart Managed switches."""

from .exporter import (
    NetgearSwitchCollector,
    NetgearSwitchScraper,
    SwitchScrapeError,
)

__version__ = "0.1.0"
__all__ = [
    "NetgearSwitchCollector",
    "NetgearSwitchScraper",
    "SwitchScrapeError",
    "__version__",
]
