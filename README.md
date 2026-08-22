# netgear-plus-exporter

Prometheus exporter for **NETGEAR Plus / Easy Smart Managed** switches: the
cheap web-managed ones (GS305E, GS308E, GS105Ev2, GS108Ev3, …) that deliberately
ship **without SNMP**.

It scrapes the switch's web UI using
[`py-netgear-plus`](https://github.com/foxey/py-netgear-plus), which handles the
login handshake and the per-model HTML quirks.

> **Pre-1.0: expect breaking changes in minor releases.** Metric names, labels
> and which series exist at all are still settling, and a metric that is
> wrong is worth fixing while the number of people pinned to it is small.
> From 0.2.0 on, each release lists them under **Breaking Changes** in its
> [release notes](https://github.com/dimidur/netgear-plus-exporter/releases).
> Pin an image tag and read the notes before upgrading.

## Why another one

Two exporters already exist for these switches. Both stopped at the surface the
library exposes publicly, and both are unmaintained (last releases 2020 and
2021). This one differs in three ways that matter if you actually alert on the
data:

1. **Unrounded counters.** `py_netgear_plus.get_switch_infos()` reports traffic
   as megabytes via `round(bytes * 1e-6, 2)`, quantised to 10 kB. On a quiet
   link that destroys `rate()`. This exporter reads the parser layer underneath
   it, which returns the raw integers the switch actually reported, and falls
   back to the rounded values only if that path is unavailable, reporting which
   one it used as `netgear_plus_raw_counters`.
2. **CRC errors on every port.** The library's public dict drops all but one on
   the way out, and that one is a delta; the parser returns a cumulative value
   per port. CRC is the single most useful metric these switches expose: a
   rising count means a bad cable, connector or port, and it is not derivable
   from anything else. This needs the parser path: on the fallback there is no
   usable CRC at all, see below.
3. **Port descriptions.** The label you set on a port in the switch UI is
   exported, so a dashboard can say `doorbell-camera` instead of `port 4`. For a
   Plus switch, with no SNMP and no readable MAC table, this is the only
   way to learn what is behind a port without walking to the rack.

## Metrics

| metric | type | labels | meaning |
| --- | --- | --- | --- |
| `netgear_plus_up` | gauge | `switch` | 1 when this scrape produced a full reading; see [Failure and recovery](#failure-and-recovery) |
| `netgear_plus_scrape_duration_seconds` | gauge | `switch` | time spent collecting |
| `netgear_plus_raw_counters` | gauge | `switch` | 1 = unrounded parser path, 0 = rescaled fallback |
| `netgear_plus_library_sample_interval_seconds` | gauge | `switch` | seconds between polls, as the library measures it; see below |
| `netgear_plus_switch_info` | gauge | `switch`, `model`, `firmware`, `bootloader`, `serial`, `ip` | static facts, always 1 |
| `netgear_plus_port_info` | gauge | `switch`, `port`, `description` | port metadata, always 1 |
| `netgear_plus_port_up` | gauge | `switch`, `port` | 1 when the port has link |
| `netgear_plus_port_speed_mbps` | gauge | `switch`, `port` | negotiated speed, 0 when down |
| `netgear_plus_port_rx_bytes_total` | counter | `switch`, `port` | bytes received |
| `netgear_plus_port_tx_bytes_total` | counter | `switch`, `port` | bytes transmitted |
| `netgear_plus_port_crc_errors_total` | counter | `switch`, `port` | CRC error frames; raw path only, see below |

The port description is a label on its **own** `netgear_plus_port_info` metric,
not on the numeric series. Renaming a port in the switch UI would otherwise
orphan every existing time series for that port.

**CRC is exported only on the raw path.** `get_switch_infos()` does carry a
`port_N_crc_errors` key, but it is a per-interval *delta* clamped at zero, not
a cumulative count. Three separate things are wrong with it as a counter:

- **The first interval of a failure is invisible.** Upstream reports `0`
  whenever the *previous* reading was `0`, so a port that has been clean until
  now reports nothing on the interval its cable starts failing. This is the
  same property that keeps the `speed_*` family out, below.
- **`increase()` is not a count of anything.** A burst confined to one interval
  is added back by the counter-reset correction and comes out about right; a
  burst spanning consecutive intervals silently undercounts, since `0, 3, 3, 0`
  yields 3 rather than 6; and for a cable failing at a *steady* rate, with
  every interval carrying the same non-zero delta, the series never rises and
  `increase()` returns exactly **0**, silent in the case the alert exists to
  catch.
- **Only one port has the key.** Upstream writes it outside its per-port loop,
  so the last port is the only one that gets a value.

A switch-wide `sum_port_crc_errors` does reach the public dict for every port,
but it carries the same delta pathology and collapses the per-port attribution
that is the whole diagnostic value of CRC, so it is not exported either.

On the fallback path the series is therefore **absent**, which is honest; a
lone delta wearing a `_total` suffix would not be. Alert on
`netgear_plus_raw_counters == 0` if CRC monitoring matters to you.

**The counter path is latched at the first successful scrape.** Raw and
fallback derive the same series two ways that disagree by up to 5 kB, half
the fallback's 10 kB rounding quantum, so switching mid-run can move a
counter *down*. Prometheus reads that as a counter reset, and `increase()`
credits the whole counter value as new traffic. If the raw path is lost mid-run
the exporter reports `netgear_plus_up 0` rather than switching; if the raw path
appears while the fallback is latched, it is picked up at the next restart.
`netgear_plus_raw_counters` reports the path the exported counters actually
use.

> **Changing in 0.2.0.** Two breaks, both affecting dashboards and alerts:
>
> - `netgear_plus_switch_response_seconds` becomes
>   `netgear_plus_library_sample_interval_seconds`; `0.1.1` and earlier export
>   the old name. It called the value a switch-reported response time and it
>   was never either. The value itself is unchanged.
> - `netgear_plus_port_crc_errors_total` disappears on the fallback path
>   (`netgear_plus_raw_counters 0`). `0.1.1` exported a single per-interval
>   delta there, for one port; see the CRC note above for why that was worse
>   than nothing.

`netgear_plus_library_sample_interval_seconds` is the gap between the library's
**last two polls** of the switch, measured from the end of the previous poll
to the start of this one's statistics fetch, so it runs slightly long. It is
not how long the switch took to answer, and not an age that grows between
scrapes: the same value is republished unchanged on every cached scrape until
the next real poll.

It is worth watching because it is the most direct view of the exporter's true
polling rate, which is not your scrape interval. Responses are cached for
`NETGEAR_EXPORTER_CACHE_SECONDS`, so polls are capped at one per TTL however
many things read `/metrics`, but any additional consumer (a second scraper, a
healthcheck that fetches `/metrics`) shifts when polls land and pushes the rate
towards that ceiling. Expect a value at or above the cache TTL.

**The first sample of a connection reads low** and is not a poll interval: it
spans login and setup instead. That happens once at exporter start, and again
whenever a failed poll forces a reconnect, so alert on it only in combination
with `netgear_plus_up`, never on its own. Routine session expiry does *not*
cause it: the library refreshes the cookie on the same connection.

### Not exported: the library's `port_N_speed_rx_mbytes` and friends

`py-netgear-plus` exposes `port_N_speed_rx_mbytes`, `_speed_tx_mbytes` and
`_speed_io_mbytes`: each is a byte delta between its own polls, divided by
the interval between them. They are deliberately left out:

- Exporters expose counters and Prometheus computes the rates.
  `rate(netgear_plus_port_rx_bytes_total[5m])` gives any window you ask for; a
  pre-computed gauge is one window you cannot change or align to your scrape.
- The divisor is the gap between *library* polls, which moves with the cache
  TTL and with how many things read `/metrics`. The average would be taken over
  a window that silently changes.
- The underlying delta reports `0`, not "unknown", whenever the previous
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
| `NETGEAR_EXPORTER_PASSWORD` | *(unset)* | web-UI password |
| `NETGEAR_EXPORTER_PASSWORD_FILE` | *(unset)* | file to read the password from; wins over the above |
| `NETGEAR_EXPORTER_NAME` | host | value of the `switch` label |
| `NETGEAR_EXPORTER_PORT` | `9694` | port to serve `/metrics` on |
| `NETGEAR_EXPORTER_CACHE_SECONDS` | `30` | minimum seconds between switch scrapes |
| `NETGEAR_EXPORTER_LOG_LEVEL` | `INFO` | Python log level |

`NETGEAR_EXPORTER_CACHE_SECONDS` protects the switch: each poll costs it two
page renders (four on PoE models), and Prometheus may scrape more often than a
device this small comfortably serves. Readings are served from cache in
between.

### Failure and recovery

`netgear_plus_up` is **1 when the scrape produced a full reading**, which is
not quite the same as "the switch answered". It also reports `0` when the
switch answers but the counter path is lost mid-run, and when a defect in the
exporter breaks metric construction. In all three the exposition carries
`netgear_plus_up 0` and nothing else, which is the point: a partial reading
would look like a healthy one.

**Failures are cached like successes**, so an unreachable switch costs one
attempt per interval rather than one per scrape, which matters because a poll
against a dead switch can run for over a minute and would otherwise pile up.
Two consequences before you alert on it:

- `netgear_plus_up` lags a real recovery. The floor is one cache interval;
  the actual wait is that plus the next Prometheus scrape plus the poll's own
  duration, so size an alert's `for:` well above `NETGEAR_EXPORTER_CACHE_SECONDS`.
- A scrape arriving while another is polling is served from cache rather than
  made to wait, so a `1` can be republished from a reading slightly older than
  the interval. It is never invented: with nothing cached yet the scrape
  fails, and a cached *failure* is replayed as a failure even when an older
  good reading is still held.

## Running

```bash
docker run --rm -p 9694:9694 \
  -e NETGEAR_EXPORTER_HOST=192.168.1.2 \
  -e NETGEAR_EXPORTER_PASSWORD=secret \
  -e NETGEAR_EXPORTER_NAME=basement-switch \
  dimidur/netgear-plus-exporter:0.1.1
```

Compose, reading the password from a file so it never appears in `docker inspect`:

```yaml
services:
  switch-exporter:
    image: dimidur/netgear-plus-exporter:0.1.1
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

`--once` scrapes, prints the exposition to stdout and exits, which is useful for
verifying credentials and parser compatibility against a new model:

```bash
NETGEAR_EXPORTER_HOST=192.168.1.2 NETGEAR_EXPORTER_PASSWORD=secret \
  netgear-plus-exporter --once
```

Check `netgear_plus_raw_counters` in the output: **1** means the unrounded
counter path works on your switch, **0** means it fell back to rescaled
megabyte values, in which case **no** CRC series is exported at all, rather
than a partial one (see the metrics table note).

## Useful queries

```promql
# a cable going bad: CRC errors appearing on a port
# (only meaningful while netgear_plus_raw_counters is 1; see Metrics)
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

Whatever `py-netgear-plus` supports: GS105/GS108/GS116/GS305/GS308/GS316 series
and relatives. Developed and verified against a **GS305E on firmware V1.0.0.16**
(bootloader V1.0.0.2).

Two caveats worth knowing:

- **NSDP was removed by NETGEAR in firmware 1.0.0.16.** This exporter does not
  use NSDP, so that is irrelevant here, but it means discovery-protocol tools no
  longer work on current firmware, and web scraping is the only remaining path.
- These are HTML scrapers. A firmware update can change the markup and break
  parsing. `netgear_plus_up` going to 0 while the switch is clearly reachable is
  the signature; check for a `py-netgear-plus` update first.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Note that **model support lives
upstream** in [`py-netgear-plus`](https://github.com/foxey/py-netgear-plus). If
your switch isn't recognised, that's the place to add it.

## Support

If this saved you some time, you can sponsor the work:

[![Sponsor](https://img.shields.io/badge/Sponsor-%E2%9D%A4-db61a2?logo=github-sponsors&logoColor=white)](https://github.com/sponsors/dimidur)

## License

MIT. See [LICENSE](LICENSE).
