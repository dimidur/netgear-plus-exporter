# Contributing

Thanks for looking. This is a small project with a narrow job: turn what a
NETGEAR Plus switch's web UI will admit into Prometheus metrics.

## Before you open an issue

**"My switch isn't supported"** belongs upstream. Model detection, the login
handshake and the per-model HTML parsing all live in
[`py-netgear-plus`](https://github.com/foxey/py-netgear-plus); support arrives
here through a dependency bump.

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

- switch **model**, **firmware** and **bootloader** version, all three of which
  the exposition reports in `netgear_plus_switch_info`
- the value of **`netgear_plus_raw_counters`**
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

CI runs these on Python 3.13, the version the published container is built on,
and adds `bandit`, `pip-audit`, `actionlint`, a container build and an image
scan.

Markdown is linted by a GitHub action rather than by anything in `[dev]`. With
Node installed, `npx --yes markdownlint-cli2` reproduces it. Without Node, CI is
the first place you will hear about it.

## What a good change looks like here

- **Tests run without hardware.** The suite feeds the collector canned readings
  so contributors without a switch can still work on it. If your change needs a
  real device to verify, say so in the PR and describe what you tested against.
- **Counters stay cumulative and unrounded, per port.** Rounding to megabytes
  quantises to 10 kB and wrecks `rate()` on a quiet link.
- **One request set per scrape.** These switches hold a single admin session and
  very little HTTP headroom. Read what the poll already retained; never fetch
  the same page twice.
- **Keep the guards on the upstream binding.** Counters come from non-public
  attributes of `py_netgear_plus.NetgearSwitchConnector`. The tests that pin it
  and the runtime checks that degrade rather than crash both exist because the
  failure is otherwise silent.
- **New labels belong on info metrics.** Port descriptions live on
  `netgear_plus_port_info`, not on the numeric series, so renaming a port in the
  switch UI doesn't orphan its history. Follow that pattern for anything else
  operator-editable.
- **Don't make a failure look like health.** A scrape that cannot reach the
  switch reports `netgear_plus_up 0`; a degraded counter path reports
  `netgear_plus_raw_counters 0`.
- **Comments say what the code does, where that isn't obvious.** Not why it was
  written that way, not what it used to be, and not what a refactor rejected.
  Reasoning goes in `docs/`, and a case worth explaining is usually worth a
  test named after it.

## Releases

**Label a PR `breaking-change` when it renames, removes or relabels a metric.**
That is what puts it under the *Breaking Changes* heading of the generated
release notes, which the README tells upgraders to read. A label can be added
to an already-merged PR before the release is tagged, but a commit pushed
straight to `main` has no PR to carry one and will not appear in the notes at
all.

Tagging and publishing are maintainer-only.

## License

Contributions are accepted under the [MIT License](LICENSE).
