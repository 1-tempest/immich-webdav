from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    return int(value) if value else default


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    return float(value) if value else default


@dataclass(frozen=True, slots=True)
class Settings:
    immich_url: str
    immich_external_url: str
    service_external_url: str
    cache_dir: str
    webdav_host: str
    webdav_port: int
    cache_ttl_seconds: float
    size_cache_ttl_seconds: float
    request_timeout_seconds: float
    request_max_retries: int
    page_size: int
    max_concurrent_requests: int
    size_probe_concurrency: int
    log_level: str

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            # Where the asset-serving path (immich_client.py) talks to Immich.
            # May be an internal/Docker-network address.
            immich_url=_env("IMMICH_URL", "http://immich-server:2283").rstrip("/"),
            # A real externally-reachable Immich URL, used ONLY by the /setup
            # self-service page (browsers and the identity provider's
            # redirect-URI matching need a public address). Independent of
            # IMMICH_URL by design; falls back to it only when unset. See
            # setup_app.py.
            immich_external_url=_env("IMMICH_EXTERNAL_URL", "").rstrip("/"),
            # This service's own externally-reachable base URL, used by /setup
            # for the OAuth redirect URI and the generated rclone config. Left
            # empty it's derived per request from X-Forwarded-Proto / Host.
            service_external_url=_env("SERVICE_EXTERNAL_URL", "").rstrip("/"),
            # Directory for caches worth keeping across restarts -- currently
            # just the learned asset byte-sizes (asset-sizes.json), so a
            # restart doesn't re-probe every asset in every album. Empty =
            # in-memory only. Mount a volume here to persist it.
            cache_dir=_env("CACHE_DIR", ""),
            webdav_host=_env("WEBDAV_HOST", "0.0.0.0"),
            webdav_port=_env_int("WEBDAV_PORT", 1700),
            # How long a cached listing is served before it's rechecked. The
            # album list (RootCollection) uses this as a hard TTL. An album's
            # asset list uses it as the stale-while-revalidate interval: the
            # cached listing is always served immediately, and once it's this
            # old the next request triggers a *background* check of the
            # album's cheap (assetCount, updatedAt) signature -- a full
            # re-fetch happens only if that moved. So a browse never blocks on
            # Immich once anything is cached; an added asset appears a poll or
            # two later.
            cache_ttl_seconds=_env_float("CACHE_TTL_SECONDS", 2.0),
            # Asset sizes essentially never change once written, so this is
            # cached far longer than listings -- long enough to eliminate
            # the no-keep-alive penalty for any file browsed more than once
            # in a two-week window, short enough to self-heal if an asset
            # were ever replaced in place.
            size_cache_ttl_seconds=_env_float("SIZE_CACHE_TTL_SECONDS", 14 * 24 * 60 * 60.0),
            request_timeout_seconds=_env_float("REQUEST_TIMEOUT_SECONDS", 30.0),
            request_max_retries=_env_int("REQUEST_MAX_RETRIES", 3),
            page_size=_env_int("PAGE_SIZE", 1000),
            max_concurrent_requests=_env_int("MAX_CONCURRENT_REQUESTS", 4),
            # Every asset's exact byte size is resolved once (per SIZE_CACHE
            # window) with a one-byte range probe so PROPFIND can report a real
            # Content-Length. These are tiny requests, unlike the paginated
            # search fan-out MAX_CONCURRENT_REQUESTS governs, so they run much
            # wider -- this bounds the one-time cost of a cold album listing.
            size_probe_concurrency=_env_int("SIZE_PROBE_CONCURRENCY", 32),
            log_level=_env("LOG_LEVEL", "INFO").upper(),
        )
