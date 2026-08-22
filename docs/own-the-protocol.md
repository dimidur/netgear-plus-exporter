# The switch protocol

What gets built, and what it has to do. Decided 2026-08-11, language
confirmed 2026-08-14.

## What we are building

A Prometheus exporter for NETGEAR Plus switches, in **Go**, speaking the
switch's web UI directly. Written from zero against the hardware.

Go for the reasons that decided it for the Kea exporter. A static binary runs
on distroless `static`: no shell, no package manager, no libc, so the image
contains our code and nothing else. The current Python image ships an
interpreter and a package manager, and the package manager's vendored
dependencies have raised CVE-gate findings on code this exporter never
executes.

Keeping the number of languages in the homelab small is a motivation of its
own. One toolchain to keep current, one set of build and scan conventions,
one place to learn a lint rule. That the sibling exporter is already Go makes
this convenient rather than being the argument for it.

## The protocol

Everything here was read from the device itself: its served HTML, its
JavaScript, and its responses. Nothing was taken from any existing
implementation, and nothing should be.

### Authentication

Confirmed against a GS305E on firmware V1.0.0.16.

`GET /` answers 200 with a body that redirects to `/login.cgi`. There is no
`WWW-Authenticate` header, so this is not HTTP Basic.

1. `GET /login.cgi` and read the `rand` value from the page.
2. Interleave the password and that value one character at a time, taking
   from each in turn until both are exhausted.
3. MD5 the interleaved string, and POST it as the `password` field to
   `/login.cgi`.
4. Carry the session cookie on every later request.

The page loads `jquery.md5.js` and `login.js`; `encryptPwd()` and `merge()`
in the latter are the authoritative statement of the above.

### The page set

**Still to be captured.** Only the login flow has been read from the device
so far. The pages carrying port status, port statistics and switch
identification must be discovered by browsing the UI and recording what it
serves, not by consulting any other implementation.

Capture them in one session and commit the bytes as fixtures. Everything
after that runs offline.

### The device is fragile

A first-class constraint, not a footnote. During a login probe the switch
began answering `Empty reply from server` after roughly six requests, while
an exporter was polling it every 30 seconds. These switches hold a single
admin session and have very little HTTP headroom.

What follows from it:

- One session, reused, re-logging in only when it expires.
- No speculative or duplicate requests. A steady-state poll of a GS305E is
  two page fetches; anything above that is a real cost, not a rounding
  error.
- Never two clients against one switch, including during testing.
- Cache readings and serve `/metrics` from cache, so scrape frequency does
  not reach the device.

## What must be implemented

### The metric contract to preserve

These are in production and carry dashboards and alerts. Names, labels and
types carry over unchanged:

| metric | type | labels |
| --- | --- | --- |
| `netgear_plus_up` | gauge | `switch` |
| `netgear_plus_scrape_duration_seconds` | gauge | `switch` |
| `netgear_plus_switch_info` | gauge | `switch`, `model`, `firmware`, `bootloader`, `serial`, `ip` |
| `netgear_plus_port_info` | gauge | `switch`, `port`, `description` |
| `netgear_plus_port_up` | gauge | `switch`, `port` |
| `netgear_plus_port_speed_mbps` | gauge | `switch`, `port` |
| `netgear_plus_port_rx_bytes_total` | counter | `switch`, `port` |
| `netgear_plus_port_tx_bytes_total` | counter | `switch`, `port` |
| `netgear_plus_port_crc_errors_total` | counter | `switch`, `port` |

`netgear_plus_scrape_duration_seconds` is how long the last poll of the switch
took. A collect served from cache republishes that value unchanged: it is not
re-measured and not zeroed, so the series is the poll cost rather than the
scrape cost.

Two metrics do **not** carry over, because both describe the old
implementation rather than the switch:

- `netgear_plus_raw_counters` reported which of two derivations produced the
  counters. There is one derivation now.
- `netgear_plus_library_sample_interval_seconds` reported the gap between
  polls as a third party measured it.

Both are a breaking change and belong under **Breaking Changes** in the
release that ships this.

### Metrics the rewrite adds

Today a failed scrape is a single `netgear_plus_up 0` covering every cause,
with the detail reaching only the log. These three are worth having whatever
the exporter is written in, so they are specified here rather than retrofitted
onto the implementation being replaced:

| metric | type | labels |
| --- | --- | --- |
| `netgear_plus_scrape_errors_total` | counter | `switch`, `reason` |
| `netgear_plus_last_success_timestamp_seconds` | gauge | `switch` |
| `netgear_plus_ports` | gauge | `switch` |

`reason` is a small closed set decided at the point of failure: `connect`,
`login`, `empty`, `parse`, `unknown`. It must stay small, since it is a label.

`netgear_plus_scrape_errors_total` counts **attempts against the device**, not
scrapes served. A collect that returns a cached failure without touching the
switch does not increment it, or the metric measures Prometheus's scrape
interval rather than the failure rate it exists to alert on.

`netgear_plus_ports` exists so that "no ports were found" is a value rather
than an absence. Today a reading that yields no ports emits no port series at
all, and `count(netgear_plus_port_up)` over a series that never appeared is
absent, not zero, so there is nothing for an alert to compare against. A gauge
that is always emitted gives one.

Reading age is deliberately not a metric.
`time() - netgear_plus_last_success_timestamp_seconds` is the same number, and
a gauge that has to be recomputed on every scrape of a cached reading is a
second thing to keep correct.

### Behaviour that is not negotiable

- **Counters are cumulative and unrounded, per port.** Rounding to megabytes
  quantises to 10 kB and destroys `rate()` on a quiet link. A per-interval
  delta is not a counter and must never wear a `_total` suffix.
- **CRC per port, or not at all.** A rising CRC count is the single most
  useful thing these switches expose, and it is not derivable from anything
  else. A switch-wide total collapses the attribution that makes it useful.
- **A degraded scrape must look degraded.** `netgear_plus_up 0` on failure.
  Never publish a reading that might be a parse break: a truncated page can
  arrive well-formed and zeroed, so all-zero byte counters following a
  non-zero reading fail the scrape rather than export.
- **Port descriptions are labels on `netgear_plus_port_info`**, never on a
  numeric series, so renaming a port in the switch UI does not orphan its
  history.
- **An unreachable switch must not queue collectors behind it.** Serving
  `/metrics` is concurrent, and a poll against an unreachable switch blocks
  for the full connect timeout on every request it makes, so a collect that
  cannot take the poll lock replays the last attempt's outcome instead of
  waiting for one.
- **A recorded failure outranks a reading taken before it.** The reading is
  kept, as the counter history the zero-transition guard compares against,
  but replaying it would report a dead switch as up. With no attempt at all
  yet, the cold-start case, the scrape fails rather than inventing one.
- **Failures are cached under the same TTL as successes**, so a dead switch
  costs one attempt per interval rather than one per scrape. That is the
  caching rule under **The device is fragile** applied to failures, which are
  otherwise the one case that reaches the device on every scrape. The clock
  starts when an attempt finishes, so the real gap is the poll's own duration
  plus the TTL.

### Structure

Three packages, one reason to change each. The Python exporter reached five
in a single module before it was split; this says where the seams go so that
does not repeat.

- **device binding**: the HTTP session, the login handshake, fetching and
  parsing pages. Changes when the switch or its firmware changes.
- **polling**: session lifecycle, the cache, the lock, the failure policy.
  Changes when the retry or caching policy changes.
- **metrics**: translation into the contract above. Changes when that
  contract changes.

Metric translation must not import the device binding **and must not use its
key names**. Cutting only the import is what the Python exporter did, and it
leaves a firmware rename as a two-module edit that no test can see: the
reading crossing the seam should already be in this exporter's own
vocabulary.

### Testing

Captured HTML is the regression suite. Parsing is the thing most likely to
break, because NETGEAR rewrites the UI without warning, and it can only be
tested against real bytes. Every test runs offline against fixtures; none
needs a switch.

## Constraints

- **No third-party implementation is consulted.** The device is a complete
  and authoritative source. This is a clean-room implementation and stays
  one.
- **One implementation at a time.** Not two drivers, not a compatibility
  shim, not a runtime switch. The current exporter runs until this replaces
  it, then it is deleted.
- **Never two clients against one switch.**
- **Fixtures are captured once and used offline.**

## Sequencing

1. **Keep the current exporter supplying telemetry.** It is the source of
   truth for the metric contract above until it is replaced.
2. **Capture the page set** from the test switch, in one session.
3. **Build against those fixtures**, offline.
4. **Verify against the test switch**, never the production one.
5. **Cut over in a single change**, and delete what it replaces.

## Hardware

| Role | Model | Firmware | Notes |
| --- | --- | --- | --- |
| Production | GS305E | V1.0.0.16 | on the IoT network, scraped every 30s |
| Test | second switch | to confirm | direct-connected to the workstation; record model and firmware here once attached |

NSDP was removed by NETGEAR in firmware 1.0.0.16, so the web UI is the only
remaining path on current firmware.
