from __future__ import annotations

from typing import Any

from wsgidav.dav_provider import DAVProvider

from immich_webdav.cache import SwrCache, TTLCache
from immich_webdav.immich_client import ImmichClient
from immich_webdav.webdav.resources import RootCollection


class ImmichProvider(DAVProvider):
    """Stateless, per-request DAV provider.

    No global catalog -- every browse is served per authenticated API key,
    which keeps album/asset visibility scoped to exactly what that key can
    see in Immich, for free. Listings are cached (see cache.py); an album's
    asset list is refreshed by a short-lived background thread only when its
    Immich signature has moved (SwrCache), never on the request path.
    """

    def __init__(
        self,
        client: ImmichClient,
        cache: TTLCache,
        size_cache: TTLCache,
        asset_cache: SwrCache,
    ) -> None:
        super().__init__()
        self._client = client
        self._cache = cache
        self._size_cache = size_cache
        self._asset_cache = asset_cache

    def is_readonly(self) -> bool:
        return True

    def get_resource_inst(self, path: str, environ: dict[str, Any]):
        api_key = environ.get("immich_webdav.api_key")
        if not api_key:
            return None
        root = RootCollection(
            environ,
            self._client,
            self._cache,
            self._size_cache,
            self._asset_cache,
            api_key,
        )
        # Validate the key up front (cached, cheap) so a bad one fails on the
        # very first request rather than resolving "/" and leaving a client
        # with a mapped-but-empty drive.
        root.check_access()
        return root.resolve("", path)
