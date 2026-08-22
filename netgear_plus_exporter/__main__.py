"""Entry point: serve /metrics for a single NETGEAR Plus switch."""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from types import FrameType

from prometheus_client import CollectorRegistry, generate_latest, start_http_server

from .collector import NetgearSwitchCollector
from .scraper import NetgearSwitchScraper

_LOGGER = logging.getLogger("netgear_plus_exporter")


class ConfigError(Exception):
    """A required setting is missing or unusable.

    Raised rather than exited on, so every configuration fault leaves through
    one path in main(): the configured log format, at ERROR, with one exit
    code. Exiting from where the fault is found bypasses both.
    """


def _read_host() -> str:
    host = os.environ.get("NETGEAR_EXPORTER_HOST")
    if not host:
        msg = "NETGEAR_EXPORTER_HOST is required (switch IP or hostname)"
        raise ConfigError(msg)
    return host


def _read_password() -> str:
    """Read the switch password, preferring the *_FILE form used by secrets.

    An unreadable or empty secrets file otherwise surfaces much later as a
    rejected login, which reports a configuration mistake as a bad password.
    """
    path = os.environ.get("NETGEAR_EXPORTER_PASSWORD_FILE")
    if path:
        try:
            with open(path, encoding="utf-8") as handle:
                from_file = handle.read().strip()
        except (OSError, UnicodeDecodeError) as exc:
            # UnicodeDecodeError is a ValueError, not an OSError, and a secrets
            # file written by something that did not expect to be read as text
            # is a configuration mistake like any other.
            msg = f"cannot read NETGEAR_EXPORTER_PASSWORD_FILE {path}: {exc}"
            raise ConfigError(msg) from exc
        if not from_file:
            msg = f"NETGEAR_EXPORTER_PASSWORD_FILE {path} is empty"
            raise ConfigError(msg)
        return from_file
    password = os.environ.get("NETGEAR_EXPORTER_PASSWORD")
    if not password:
        msg = (
            "set NETGEAR_EXPORTER_PASSWORD or NETGEAR_EXPORTER_PASSWORD_FILE "
            "to the switch web-UI password"
        )
        raise ConfigError(msg)
    return password


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

    try:
        host = _read_host()
        password = _read_password()
    except ConfigError as exc:
        _LOGGER.error("%s", exc)
        return 2

    listen_port = int(_env_float("NETGEAR_EXPORTER_PORT", 9694))
    switch = os.environ.get("NETGEAR_EXPORTER_NAME") or host
    scraper = NetgearSwitchScraper(
        host=host,
        password=password,
        cache_seconds=_env_float("NETGEAR_EXPORTER_CACHE_SECONDS", 30.0),
    )
    # A private registry, not the global default one: keeps the output to this
    # exporter's own metrics with no process/GC collectors mixed in.
    registry = CollectorRegistry()
    registry.register(NetgearSwitchCollector(scraper, switch=switch, host=host))

    # --once: scrape, print the exposition to stdout, exit. Lets you verify a
    # switch without running a server, and gives CI a smoke test.
    if "--once" in sys.argv:
        sys.stdout.write(generate_latest(registry).decode())
        return 0

    start_http_server(listen_port, registry=registry)
    _LOGGER.info("serving metrics for %s on :%d", switch, listen_port)

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
