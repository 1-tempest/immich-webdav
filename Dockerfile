# syntax=docker/dockerfile:1@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32
FROM python:3.14-slim@sha256:cae66f2ef0ec51a9891263eeee7f987dacf0a9879e8aa9353d5606e0530619a5 AS builder

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir --prefix=/install .


FROM python:3.14-slim@sha256:cae66f2ef0ec51a9891263eeee7f987dacf0a9879e8aa9353d5606e0530619a5

RUN useradd --create-home --uid 10001 immich-webdav \
    && mkdir /cache \
    && chown immich-webdav:immich-webdav /cache
COPY --from=builder /install /usr/local

USER immich-webdav
WORKDIR /home/immich-webdav

# /cache holds the learned per-asset byte sizes, so a restart doesn't
# re-probe every album. It lives in the container layer unless you mount a
# volume at /cache (recommended) to keep it across `docker rm` too. A bind
# mount here must be writable by uid 10001.
ENV WEBDAV_HOST=0.0.0.0 \
    WEBDAV_PORT=1700 \
    CACHE_DIR=/cache \
    PYTHONUNBUFFERED=1

EXPOSE 1700

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import socket; socket.create_connection(('127.0.0.1', 1700), timeout=3).close()" || exit 1

ENTRYPOINT ["immich-webdav"]
