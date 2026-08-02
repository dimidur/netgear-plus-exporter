"""Entry point: serve /metrics for a single NETGEAR Plus switch."""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from types import FrameType

from prometheus_client import CollectorRegistry, generate_latest, start_http_server

from .exporter import NetgearSwitchCollector, NetgearSwitchScraper, read_password

_LOGGER = logging.getLogger("netgear_plus_exporter")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        _LOGGER.warning("%s is not a number; using %s", name, default)
        return default


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("NETGEAR_EXPORTER_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    host = os.environ.get("NETGEAR_EXPORTER_HOST")
    if not host:
        _LOGGER.error("NETGEAR_EXPORTER_HOST is required (switch IP or hostname)")
        return 2

    listen_port = int(_env_float("NETGEAR_EXPORTER_PORT", 9694))
    scraper = NetgearSwitchScraper(
        host=host,
        password=read_password(),
        name=os.environ.get("NETGEAR_EXPORTER_NAME") or host,
        cache_seconds=_env_float("NETGEAR_EXPORTER_CACHE_SECONDS", 30.0),
    )
    # A private registry, not the global default one: keeps the output to this
    # exporter's own metrics with no process/GC collectors mixed in.
    registry = CollectorRegistry()
    registry.register(NetgearSwitchCollector(scraper))

    # --once: scrape, print the exposition to stdout, exit. Lets you verify a
    # switch without running a server, and gives CI a smoke test.
    if "--once" in sys.argv:
        sys.stdout.write(generate_latest(registry).decode())
        return 0

    start_http_server(listen_port, registry=registry)
    _LOGGER.info("serving metrics for %s on :%d", scraper.name, listen_port)

    # The exporter is entirely pull-driven; idle until told to stop. Handling
    # SIGTERM explicitly keeps `docker stop` from taking the full 10s grace.
    stopping = False

    def _stop(signum: int, _frame: FrameType | None) -> None:
        nonlocal stopping
        _LOGGER.info("signal %s received, shutting down", signum)
        stopping = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    while not stopping:
        time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
