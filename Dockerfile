# syntax=docker/dockerfile:1
FROM python:3.12-slim AS build

WORKDIR /src
COPY pyproject.toml README.md ./
COPY netgear_plus_exporter ./netgear_plus_exporter
RUN python -m pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim

# lxml (pulled in by py-netgear-plus) needs libxml2/libxslt at runtime; the
# slim image ships neither.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libxml2 libxslt1.1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /install /usr/local

# Unprivileged: the exporter only makes outbound HTTP calls and binds one port.
RUN useradd --system --uid 65533 --no-create-home exporter
USER 65533

ENV NETGEAR_EXPORTER_PORT=9694 \
    PYTHONUNBUFFERED=1

EXPOSE 9694
ENTRYPOINT ["netgear-plus-exporter"]
