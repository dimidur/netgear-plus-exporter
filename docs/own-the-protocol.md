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

Two metrics do **not** carry over, because both describe the old
implementation rather than the switch:

- `netgear_plus_raw_counters` reported which of two derivations produced the
  counters. There is one derivation now.
- `netgear_plus_library_sample_interval_seconds` reported the gap between
  polls as a third party measured it.

Both are a breaking change and belong under **Breaking Changes** in the
release that ships this.

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
