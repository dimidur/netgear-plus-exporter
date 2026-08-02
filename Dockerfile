# syntax=docker/dockerfile:1
#
# Alpine, single stage. Measured against the alternatives on 2026-08-02:
#
#   python:3.13-alpine   71 MB   0C  0H  0M  0L   <- this
#   python:3.12-alpine    74 MB   0C  0H  4M  1L
#   python:3.12-slim     145 MB   1C  2H  7M 29L
#
# No system packages are needed: lxml (pulled in by py-netgear-plus) ships
# musllinux wheels with libxml2/libxslt statically bundled, for x86_64 AND
# aarch64 -- so nothing compiles from source on either published architecture.
# An earlier slim-based attempt apt-installed libxml2/libxslt for no reason.
FROM python:3.13-alpine

WORKDIR /app

COPY pyproject.toml README.md ./
COPY netgear_plus_exporter ./netgear_plus_exporter
RUN python -m pip install --no-cache-dir .

# Unprivileged: the exporter only makes outbound HTTP calls and binds one port.
RUN adduser -S -u 65533 exporter
USER 65533

ENV NETGEAR_EXPORTER_PORT=9694 \
    PYTHONUNBUFFERED=1

EXPOSE 9694
ENTRYPOINT ["netgear-plus-exporter"]
