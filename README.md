# netgear-plus-exporter

Prometheus exporter for **NETGEAR Plus / Easy Smart Managed** switches — the
cheap web-managed ones (GS305E, GS308E, GS105Ev2, GS108Ev3, …) that deliberately
ship **without SNMP**.

It scrapes the switch's web UI using
[`py-netgear-plus`](https://github.com/foxey/py-netgear-plus), which handles the
login handshake and the per-model HTML quirks.

## Why another one

Two exporters already exist for these switches. Both stopped at the surface the
library exposes publicly, and both are unmaintained (last releases 2020 and
2021). This one differs in three ways that matter if you actually alert on the
data:

1. **Unrounded counters.** `py_netgear_plus.get_switch_infos()` reports traffic
   as megabytes via `round(bytes * 1e-6, 2)` — quantised to 10 kB. On a quiet
   link that destroys `rate()`. This exporter reads the parser layer underneath
   it, which returns the raw integers the switch actually reported, and falls
   back to the rounded values only if that path is unavailable — reporting which
   one it used as `netgear_plus_raw_counters`.
2. **CRC errors on every port.** The library's public dict drops most of them on
   the way out; the parser returns one value per port. CRC is the single most
   useful metric these switches expose — a rising count means a bad cable,
   connector or port, and it is not derivable from anything else.
3. **Port descriptions.** The label you set on a port in the switch UI is
   exported, so a dashboard can say `doorbell-camera` instead of `port 4`. For a
   Plus switch — no SNMP, no readable MAC table — this is the only way to learn
   what is behind a port without walking to the rack.

## Metrics

| metric | type | labels | meaning |
| --- | --- | --- | --- |
| `netgear_plus_up` | gauge | `switch` | 1 if the last scrape succeeded |
| `netgear_plus_scrape_duration_seconds` | gauge | `switch` | time spent collecting |
| `netgear_plus_raw_counters` | gauge | `switch` | 1 = unrounded parser path, 0 = rescaled fallback |
| `netgear_plus_library_sample_interval_seconds` | gauge | `switch` | seconds between polls, as the library measures it — see below |
| `netgear_plus_switch_info` | gauge | `switch`, `model`, `firmware`, `bootloader`, `serial`, `ip` | static facts, always 1 |
| `netgear_plus_port_info` | gauge | `switch`, `port`, `description` | port metadata, always 1 |
| `netgear_plus_port_up` | gauge | `switch`, `port` | 1 when the port has link |
| `netgear_plus_port_speed_mbps` | gauge | `switch`, `port` | negotiated speed, 0 when down |
| `netgear_plus_port_rx_bytes_total` | counter | `switch`, `port` | bytes received |
| `netgear_plus_port_tx_bytes_total` | counter | `switch`, `port` | bytes transmitted |
| `netgear_plus_port_crc_errors_total` | counter | `switch`, `port` | CRC error frames |

The port description is a label on its **own** `netgear_plus_port_info` metric,
not on the numeric series. Renaming a port in the switch UI would otherwise
orphan every existing time series for that port.

> **Renaming in 0.2.0.** `netgear_plus_switch_response_seconds` becomes
> `netgear_plus_library_sample_interval_seconds`; `0.1.1` and earlier export
> the old name. It called the value a switch-reported response time and it was
> never either. Update any dashboard or alert referring to it — the value
> itself is unchanged.

`netgear_plus_library_sample_interval_seconds` is the gap between the library's
**last two polls** of the switch — measured from the end of the previous poll
to the start of this one's statistics fetch, so it runs slightly long. It is
not how long the switch took to answer, and not an age that grows between
scrapes: the same value is republished unchanged on every cached scrape until
the next real poll.

It is worth watching because it is the most direct view of the exporter's true
polling rate, which is not your scrape interval. Responses are cached for
`NETGEAR_EXPORTER_CACHE_SECONDS`, so polls are capped at one per TTL however
many things read `/metrics` — but any additional consumer (a second scraper, a
healthcheck that fetches `/metrics`) shifts when polls land and pushes the rate
towards that ceiling. Expect a value at or above the cache TTL.

**The first sample of a connection reads low** and is not a poll interval: it
spans login and setup instead. That happens once at exporter start, and again
whenever a failed poll forces a reconnect — so alert on it only in combination
with `netgear_plus_up`, never on its own. Routine session expiry does *not*
cause it: the library refreshes the cookie on the same connection.

### Not exported: the library's `port_N_speed_rx_mbytes` and friends

`py-netgear-plus` exposes `port_N_speed_rx_mbytes`, `_speed_tx_mbytes` and
`_speed_io_mbytes` — a byte delta between its own polls, divided by the
interval between them. They are deliberately left out:

- Exporters expose counters and Prometheus computes the rates.
  `rate(netgear_plus_port_rx_bytes_total[5m])` gives any window you ask for; a
  pre-computed gauge is one window you cannot change or align to your scrape.
- The divisor is the gap between *library* polls, which moves with the cache
  TTL and with how many things read `/metrics`. The average would be taken over
  a window that silently changes.
- The underlying delta reports `0` — not "unknown" — whenever the previous
  reading was 0. So the first sample of any connection looks like idle traffic,
  as does any port whose counter was still 0 last time.
- They exist only in the `_mbytes` form, through the same
  `round(v * 1e-6, 2)` that reason 1 at the top of this README rejects for
  counters. Exporting them would reintroduce the 10 kB quantisation this
  project exists to avoid.

Note `netgear_plus_port_speed_mbps` is unrelated: it is the negotiated link
speed, not a throughput.

## Configuration

All configuration is environment variables. One container per switch.

| variable | default | meaning |
| --- | --- | --- |
| `NETGEAR_EXPORTER_HOST` | *(required)* | switch IP or hostname |
| `NETGEAR_EXPORTER_PASSWORD` | — | web-UI password |
| `NETGEAR_EXPORTER_PASSWORD_FILE` | — | file to read the password from; wins over the above |
| `NETGEAR_EXPORTER_NAME` | host | value of the `switch` label |
| `NETGEAR_EXPORTER_PORT` | `9694` | port to serve `/metrics` on |
| `NETGEAR_EXPORTER_CACHE_SECONDS` | `30` | minimum seconds between switch scrapes |
| `NETGEAR_EXPORTER_LOG_LEVEL` | `INFO` | Python log level |

`NETGEAR_EXPORTER_CACHE_SECONDS` protects the switch: each poll costs it two
page renders (four on PoE models), and Prometheus may scrape more often than a
device this small comfortably serves. Readings are served from cache in
between.

## Running

```bash
docker run --rm -p 9694:9694 \
  -e NETGEAR_EXPORTER_HOST=192.168.1.2 \
  -e NETGEAR_EXPORTER_PASSWORD=secret \
  -e NETGEAR_EXPORTER_NAME=basement-switch \
  dimidur/netgear-plus-exporter:0.2.0
```

Compose, reading the password from a file so it never appears in `docker inspect`:

```yaml
services:
  switch-exporter:
    image: dimidur/netgear-plus-exporter:0.2.0
    restart: unless-stopped
    environment:
      NETGEAR_EXPORTER_HOST: 192.168.1.2
      NETGEAR_EXPORTER_NAME: basement-switch
      NETGEAR_EXPORTER_PASSWORD_FILE: /run/secrets/switch_password
    volumes:
      - ./switch_password:/run/secrets/switch_password:ro
```

Prometheus:

```yaml
scrape_configs:
  - job_name: netgear-switches
    scrape_interval: 60s
    static_configs:
      - targets: ["switch-exporter:9694"]
```

### Checking a switch without running a server

`--once` scrapes, prints the exposition to stdout and exits — useful for
verifying credentials and parser compatibility against a new model:

```bash
NETGEAR_EXPORTER_HOST=192.168.1.2 NETGEAR_EXPORTER_PASSWORD=secret \
  netgear-plus-exporter --once
```

Check `netgear_plus_raw_counters` in the output: **1** means the unrounded
counter path works on your switch, **0** means it fell back to rescaled megabyte
values and CRC data will be incomplete.

## Useful queries

```promql
# a cable going bad: CRC errors appearing on a port
increase(netgear_plus_port_crc_errors_total[1h]) > 0

# a gigabit-capable link that negotiated 100 Mbit
netgear_plus_port_speed_mbps == 100

# throughput per port, joined to the human name set in the switch UI
rate(netgear_plus_port_rx_bytes_total[5m])
  * on(switch, port) group_left(description) netgear_plus_port_info

# firmware changed under you
changes(netgear_plus_switch_info[7d]) > 0
```

## Supported hardware

Whatever `py-netgear-plus` supports — GS105/GS108/GS116/GS305/GS308/GS316 series
and relatives. Developed and verified against a **GS305E on firmware V1.0.0.16**
(bootloader V1.0.0.2).

Two caveats worth knowing:

- **NSDP was removed by NETGEAR in firmware 1.0.0.16.** This exporter does not
  use NSDP, so that is irrelevant here — but it means discovery-protocol tools no
  longer work on current firmware, and web scraping is the only remaining path.
- These are HTML scrapers. A firmware update can change the markup and break
  parsing. `netgear_plus_up` going to 0 while the switch is clearly reachable is
  the signature; check for a `py-netgear-plus` update first.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
ruff check .
pytest
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Note that **model support lives
upstream** in [`py-netgear-plus`](https://github.com/foxey/py-netgear-plus) — if
your switch isn't recognised, that's the place to add it.

## Support

If this saved you some time, you can sponsor the work:

[![Sponsor](https://img.shields.io/badge/Sponsor-%E2%9D%A4-db61a2?logo=github-sponsors&logoColor=white)](https://github.com/sponsors/dimidur)

## License

MIT — see [LICENSE](LICENSE).
