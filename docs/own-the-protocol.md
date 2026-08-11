# Owning the switch protocol

Decided 2026-08-11.

**We will own the switch protocol end to end and drop `py-netgear-plus`.**
Not a fork, not a second backend. From zero, clean-room, against the
hardware.

This reverses the original call. The dependency was right for getting a
working exporter quickly and for learning what these switches expose. It is
no longer the right fit for where this project is going.

## Why

### The part we depend on is not the part the library offers

This exporter binds to `NetgearSwitchConnector._get_port_statistics()`, a
private method, because the public API returns byte counters rounded to
megabytes and CRC as a one-port per-interval delta. Those are reasonable
choices for the library's main consumer; they are simply not the values a
Prometheus counter can be built from. A private binding carries no semver
protection, so any release can change it and we find out at runtime.

That binding is guarded two ways and documented as a conscious trade in
CONTRIBUTING. That is the tell. We already half-own the protocol while
carrying the full cost of not owning it.

### Three behaviours we cannot change from outside

| Issue | What |
| --- | --- |
| #14 | Each scrape fetches the statistics page twice, following the library's call pattern. |
| #15 | The public CRC value is a per-interval delta, clamped at zero, for one port. |
| #17 | `convert_to_int` pads short lists with zeros, so a partial parse break reads as healthy zeros. |

Changing any of these means opening an upstream discussion, waiting for a
release, then revalidating the private binding against it. That is a
reasonable process; it is just a long loop for a contract we depend on
closely.

### The thing most likely to break is the thing we cannot test

The tests feed the collector canned dictionaries. They pin translation and
cannot catch a parse break, because we never parse. The stated failure mode
in the README is a firmware update changing the markup, and there is no
coverage for it at all.

Owning the protocol makes captured HTML the regression suite. That asset is
unavailable at any price today.

### Honest counterweight

Most of the friction is interface fit, not parsing quality. The parser layer
returns correct raw integers, and the public API is a sensible shape for the
consumer it was built for: `py-netgear-plus` primarily serves Home Assistant,
where rounded megabytes and per-interval deltas are exactly what a dashboard
wants. Prometheus needs the opposite contract, cumulative and unrounded and
per-port, and that is a genuinely awkward fit rather than a shortcoming in
the library.

So the gain is control of the data model, not dramatically better parsing.

Firmware updates will still break us. We would own the fix rather than wait
for one, which is better, but it is a transfer of work rather than an
elimination of it.

## What the protocol actually is

Established 2026-08-11 by probing the live GS305E on firmware V1.0.0.16.
Everything below came from the device's own `login.cgi` and `login.js`.

### Authentication

Not HTTP Basic. `GET /` answers 200 with a redirect to `/login.cgi` and no
`WWW-Authenticate` header. The flow is:

1. `GET /login.cgi` and read the `rand` seed from the page.
2. `merge(password, rand)`, interleaving the two strings one character at a
   time until both are exhausted.
3. `md5()` the merged string and POST it as the `password` field.
4. Carry the session cookie thereafter.

Roughly fifteen lines of Python: `hashlib.md5` and a two-line interleave.
This is the whole of what the dependency was encapsulating.

### The device is fragile under load

**A first-class design constraint, not a footnote.** During the probe the
switch began returning `Empty reply from server` after roughly six requests,
while the production exporter was polling it every 30 seconds. These Plus
switches hold a single admin session and have very little HTTP headroom.

Consequences for the driver:

- One session, reused, with careful re-login on expiry.
- No speculative or duplicate fetches. This is why #14 matters more than a
  wasted round trip suggests: it doubles the draw on a device with no
  headroom.
- Never two clients against one switch at once.

## Sequencing

**Stabilise first, then replace. There is never a point at which two drivers
exist.**

### Phase 1: stabilise what runs today

Close the known issues against current firmware and let the existing exporter
keep supplying telemetry. Remaining: #14, #19, #20, #21, #22.

The adapter boundary in #22 is worth doing on its own merits and makes the
eventual cutover a contained change rather than a rewrite.

### Phase 2: build the driver from zero

Against a dedicated test switch, never the live one. A second switch will be
connected directly to the workstation, so experiments cannot disturb
production telemetry or contend for the live switch's single session.

Capture the page set as fixtures in one careful session, then work offline
against those bytes. Validation is a parser diff against captured HTML, never
two live clients.

### Phase 3: cut over

Replace the dependency in a single change and delete it. No compatibility
shim, no second backend, no runtime switch between implementations.

## Standing constraints

- **No upstream intellectual property in the driver.** Everything is derived
  from the device: its served HTML, its JavaScript, and observed responses.
  Upstream is Apache 2.0, so derivation would be legally permissible with
  attribution, but we are not doing it. The device is a complete and
  authoritative source, so there is no reason to read the implementation.
- **Never two drivers.** Not in production, not in test, not behind a flag.
- **Never two clients against one switch.** Single admin session.
- **Fixtures are captured once and used offline.** Every experiment that can
  run without hardware, does.

## Hardware

| Role | Model | Firmware | Notes |
| --- | --- | --- | --- |
| Production | GS305E | V1.0.0.16 | on the IoT network, scraped every 30s |
| Test | second switch | to confirm | direct-connected to the workstation; model and firmware to be recorded here once attached |

NSDP was removed by NETGEAR in firmware 1.0.0.16, so web scraping is the only
remaining path on current firmware. This exporter never used NSDP.
