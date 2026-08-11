# Contributing

Thanks for looking. This is a small project with a narrow job: turn what a
NETGEAR Plus switch's web UI will admit into Prometheus metrics.

## Before you open an issue

**"My switch isn't supported"** is almost always an upstream matter.
Model detection, the login handshake and the per-model HTML parsing all live in
[`py-netgear-plus`](https://github.com/foxey/py-netgear-plus); this project only
translates its output into metrics. If your model is missing there, that is the
place to add it, and support then arrives here via a dependency bump.

**"It broke after a firmware update"** is expected occasionally: these are HTML
scrapers and NETGEAR rewrites the UI without warning. Check for a
`py-netgear-plus` release first.

## Reporting a problem

Run the exporter once and include the output:

```bash
NETGEAR_EXPORTER_HOST=<switch-ip> NETGEAR_EXPORTER_PASSWORD=<password> \
  netgear-plus-exporter --once
```

Please include:

- switch **model**, **firmware** and **bootloader** version (the exposition
  reports all three in `netgear_plus_switch_info`)
- the value of **`netgear_plus_raw_counters`**: `0` means the unrounded counter
  path failed on your switch and it fell back to rescaled megabytes, which is a
  distinct bug class from "no metrics at all"
- the exposition itself, with anything you consider sensitive redacted

Metrics carry no credentials, but they do include your port descriptions,
switch serial and network shape. Redact as you see fit.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
ruff check . && ruff format --check .
mypy netgear_plus_exporter
yamllint .
pytest
```

CI runs these on Python 3.13 alone, the version the published container is
built on, and adds `bandit`, `pip-audit`, `actionlint` over the workflows, a
container build and an image scan. `pytest` also covers the shape of
`.github/release.yml`, which decides whether a release's notes carry a
**Breaking Changes** heading.

Markdown is linted too, by a GitHub action rather than anything in `[dev]`.
With Node installed, `npx --yes markdownlint-cli2` reproduces it; pin the
version the action bundles, listed as its `markdownlint-cli2` dependency, if
you want an exact match. Without Node, CI is the first place you will hear
about it.

## What a good change looks like here

- **Tests run without hardware.** The suite feeds the collector canned readings
  precisely so contributors without a switch can still work on it. If your
  change needs a real device to verify, say so in the PR and describe what you
  tested against. That is fine and honest; it just cannot be automated.
- **Counters stay unrounded.** The whole reason this exporter exists rather than
  using the library's public dict is that `get_switch_infos()` reports traffic
  as `round(bytes * 1e-6, 2)`, quantising to 10 kB and wrecking `rate()` on a
  quiet link. Please don't reintroduce rounding on a counter path.
- **New labels belong on info metrics.** Port descriptions live on
  `netgear_plus_port_info`, not on the numeric series, so renaming a port in the
  switch UI doesn't orphan its history. Follow that pattern for anything else
  operator-editable.
- **Don't make a failure look like health.** A scrape that cannot reach the
  switch reports `netgear_plus_up 0`; a degraded counter path reports
  `netgear_plus_raw_counters 0`. Both are deliberate. The fallback used to be
  logged at debug, which made a fully degraded exporter look fine.

## A note on the upstream binding

Raw counters and per-port CRC come from `NetgearSwitchConnector._get_port_statistics()`,
a non-public method. That is a conscious trade: the public API rounds the byte
counters, and CRC, the metric that actually tells you a cable is failing,
reaches it only as a per-interval delta, for one port. See the CRC note in the
README.

The binding is guarded two ways: a test asserts the method still exists, and the
exporter falls back to the rounded values rather than crashing if it disappears.
If you touch that code, keep both.

## Releases

Maintainer only: tag `vX.Y.Z` and CI builds and publishes multi-arch images to
Docker Hub, then creates the GitHub release with
[automatically generated notes](https://docs.github.com/en/repositories/releasing-projects-on-github/automatically-generated-release-notes).
Base-image refreshes are automated separately, gated on a grace period and a
clean CVE scan.

The notes are grouped by pull-request label, per `.github/release.yml`. **Label
a PR `breaking-change` when it renames, removes or relabels a metric**. That is
what puts it under the *Breaking Changes* heading the README tells upgraders to
read. Labels are read when the release is generated, so one can be added to an
already-merged PR before tagging.

The generated notes list merged pull requests. A commit pushed straight to
`main` has no PR to carry a label and will not appear at all, so a breaking
change has to arrive through a PR to be announced.

## License

Contributions are accepted under the [MIT License](LICENSE).
