# Security

## Reporting a vulnerability

Please report security issues through
[GitHub's private vulnerability reporting](https://github.com/dimidur/netgear-plus-exporter/security/advisories/new)
rather than a public issue.

## What this exporter touches

Worth being explicit, because it holds a credential:

- **It holds your switch's web-UI password.** That password is usually the only
  thing protecting the switch's management interface, and on this class of
  hardware it is often the *same* password across every switch you own.
- **It logs in on a schedule.** The switch sees a continuous authenticated
  session from wherever this runs.
- **It talks plain HTTP.** These switches offer no HTTPS, so the credential
  crosses the network in whatever protection the link itself provides. Keep the
  exporter and the switch on the same trusted segment.

## Running it safely

- Prefer `NETGEAR_EXPORTER_PASSWORD_FILE` over `NETGEAR_EXPORTER_PASSWORD`. An
  environment variable is visible to anything that can run `docker inspect` on
  the host; a mounted file is not.
- Don't publish the metrics port to the world. It exposes your network's port
  topology and device names. Bind it to loopback or an internal network and let
  your scraper reach it there.
- The container runs as an unprivileged user (uid 65533), needs no capabilities,
  and only makes outbound HTTP requests plus one listening port.
- Metrics contain no credentials. They do contain switch model, firmware,
  serial number and whatever port descriptions you set.

## Supported versions

The most recent release. This is a small project — fixes go into a new release
rather than being backported.
