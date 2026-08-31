from __future__ import annotations

import logging
import os
import signal

from cheroot import wsgi
from wsgidav.wsgidav_app import WsgiDAVApp

from immich_webdav.auth import ImmichApiKeyDC
from immich_webdav.cache import SwrCache, TTLCache
from immich_webdav.config import Settings
from immich_webdav.immich_client import ImmichClient
from immich_webdav.setup_app import SETUP_ROUTE_KEYS, SetupApp
from immich_webdav.webdav.provider import ImmichProvider

logger = logging.getLogger(__name__)


def _mount_setup(dav_app, setup_app):
    """Route the setup app's exact routes (`GET /` for the page, plus
    `/setup/*` for its API; unauthenticated self-service) to `setup_app`;
    everything else -- every `PROPFIND`/`OPTIONS`/file `GET`, and e.g.
    `PROPFIND /setup` for an album named "setup" -- to the WebDAV app."""

    def app(environ, start_response):
        key = (environ.get("REQUEST_METHOD", ""), environ.get("PATH_INFO", "") or "/")
        if key in SETUP_ROUTE_KEYS:
            return setup_app(environ, start_response)
        return dav_app(environ, start_response)

    return app


def _verbosity(log_level: str) -> int:
    return {
        "CRITICAL": 0,
        "FATAL": 0,
        "ERROR": 1,
        "WARNING": 2,
        "WARN": 2,
        "INFO": 3,
        "DEBUG": 4,
    }.get(log_level, 3)


def run_webdav_server(settings: Settings | None = None) -> None:
    settings = settings or Settings.from_env()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    client = ImmichClient(
        settings.immich_url,
        timeout_seconds=settings.request_timeout_seconds,
        max_retries=settings.request_max_retries,
        page_size=settings.page_size,
        max_concurrent_requests=settings.max_concurrent_requests,
        size_probe_concurrency=settings.size_probe_concurrency,
    )
    size_cache_path = (
        os.path.join(settings.cache_dir, "asset-sizes.json")
        if settings.cache_dir
        else None
    )
    cache = TTLCache(ttl_seconds=settings.cache_ttl_seconds)
    size_cache = TTLCache(
        ttl_seconds=settings.size_cache_ttl_seconds, persist_path=size_cache_path
    )
    asset_cache = SwrCache(recheck_seconds=settings.cache_ttl_seconds)
    provider = ImmichProvider(client, cache, size_cache, asset_cache)

    config = {
        "host": settings.webdav_host,
        "port": settings.webdav_port,
        "provider_mapping": {"/": provider},
        "http_authenticator": {
            # Accepts any non-empty password and stashes it as the API key;
            # a bad key is caught by the first real Immich call. See auth.py.
            "domain_controller": ImmichApiKeyDC,
            "accept_basic": True,
            "accept_digest": False,
            "default_to_digest": False,
        },
        # No directory-listing HTML UI -- this only ever needs to speak
        # WebDAV to real clients, not present a browsable index to a browser.
        "dir_browser": {"enable": False},
        "property_manager": None,
        "lock_storage": None,
        "verbose": _verbosity(settings.log_level),
    }

    setup_external_immich = settings.immich_external_url or settings.immich_url
    if not settings.immich_external_url:
        logger.warning(
            "IMMICH_EXTERNAL_URL is unset; /setup will use IMMICH_URL (%s). "
            "Set it explicitly once IMMICH_URL points at an internal address.",
            settings.immich_url,
        )
    setup_app = SetupApp(
        immich_external_url=setup_external_immich,
        service_external_url=settings.service_external_url,
        request_timeout_seconds=settings.request_timeout_seconds,
    )

    app = _mount_setup(WsgiDAVApp(config), setup_app)
    server = wsgi.Server(
        bind_addr=(settings.webdav_host, settings.webdav_port),
        wsgi_app=app,
    )

    # SIGTERM (docker stop) as well as SIGINT should unblock server.start()
    # so the `finally` runs and the size cache is flushed to disk.
    def _shutdown(signum, _frame):
        logger.info("Received signal %d, shutting down", signum)
        server.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _shutdown)

    try:
        logger.info(
            "Starting Immich WebDAV gateway on %s:%d (Immich: %s)",
            settings.webdav_host,
            settings.webdav_port,
            settings.immich_url,
        )
        server.start()
    finally:
        server.stop()
        size_cache.flush()
