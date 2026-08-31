from __future__ import annotations

import logging
from typing import Any, Mapping

from dateutil.parser import isoparse
from wsgidav.dav_error import DAVError, HTTP_UNAUTHORIZED
from wsgidav.dav_provider import DAVCollection, DAVNonCollection
from wsgidav.util import join_uri

from immich_webdav.cache import SwrCache, TTLCache
from immich_webdav.formats import (
    FULLSIZE_CONTENT_TYPE,
    is_web_supported,
    output_extension,
)
from immich_webdav.immich_client import ImmichAuthError, ImmichClient, content_total_size

logger = logging.getLogger(__name__)

_REALM = "immich-webdav"


def _auth_challenge(exc: ImmichAuthError) -> DAVError:
    """Turn an Immich key rejection into a WebDAV 401 with a Basic challenge.

    Without the `WWW-Authenticate` header some clients treat the 401 as a hard
    failure instead of re-prompting for credentials.
    """
    return DAVError(
        HTTP_UNAUTHORIZED,
        "Immich rejected this API key",
        src_exception=exc,
        add_headers=[("WWW-Authenticate", f'Basic realm="{_REALM}"')],
    )


def _sanitize(name: str) -> str:
    name = (name or "").replace("/", "_").replace("\\", "_").strip()
    return name or "Untitled"


def _unique_name(base: str, used: set[str], unique_suffix: str) -> str:
    base = _sanitize(base)
    if base not in used:
        used.add(base)
        return base

    candidate = f"{base} [{unique_suffix}]"
    counter = 2
    while candidate in used:
        candidate = f"{base} [{unique_suffix}-{counter}]"
        counter += 1
    used.add(candidate)
    return candidate


def _strip_extension(filename: str) -> str:
    stem, sep, _ext = filename.rpartition(".")
    return stem if sep else filename


def _parse_timestamp(value: Any) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return int(isoparse(value).timestamp())
    except (TypeError, ValueError, OverflowError):
        return None


class RootCollection(DAVCollection):
    """Top-level collection: one entry per album visible to this API key."""

    def __init__(
        self,
        environ: dict[str, Any],
        client: ImmichClient,
        cache: TTLCache,
        size_cache: TTLCache,
        asset_cache: SwrCache,
        api_key: str,
    ) -> None:
        super().__init__("/", environ)
        self._client = client
        self._cache = cache
        self._size_cache = size_cache
        self._asset_cache = asset_cache
        self._api_key = api_key

    def _album_map(self) -> Mapping[str, dict]:
        def build() -> dict[str, dict]:
            albums = self._client.list_albums(self._api_key)
            used: set[str] = set()
            result: dict[str, dict] = {}
            for album in albums:
                album_id = album.get("id")
                if not isinstance(album_id, str) or not album_id:
                    continue
                name = _unique_name(str(album.get("albumName") or "Untitled Album"), used, album_id)
                result[name] = album
            return result

        try:
            return self._cache.get_or_compute((self._api_key, "__root__"), build)
        except ImmichAuthError as exc:
            raise _auth_challenge(exc) from exc

    def check_access(self) -> None:
        """Raise a WebDAV 401 if the API key is not valid.

        Called for every resolved path (see ImmichProvider.get_resource_inst)
        so a bad key fails on the *first* request -- an OPTIONS or a Depth-0
        PROPFIND of `/`, which otherwise resolve without ever contacting
        Immich, letting a client map a broken empty drive. Cheap: the album
        list is cached for CACHE_TTL_SECONDS and this is the same call the
        Depth-1 listing makes anyway.
        """
        self._album_map()

    def get_member_names(self) -> list[str]:
        return sorted(self._album_map())

    def get_member(self, name: str):
        album = self._album_map().get(name)
        if album is None:
            return None
        return AlbumCollection(
            join_uri(self.path, name),
            self.environ,
            self._client,
            self._size_cache,
            self._asset_cache,
            self._api_key,
            album,
        )


class AlbumCollection(DAVCollection):
    """One Immich album's image assets."""

    def __init__(
        self,
        path: str,
        environ: dict[str, Any],
        client: ImmichClient,
        size_cache: TTLCache,
        asset_cache: SwrCache,
        api_key: str,
        album: dict,
    ) -> None:
        super().__init__(path, environ)
        self._client = client
        self._size_cache = size_cache
        self._asset_cache = asset_cache
        self._api_key = api_key
        self._album = album

    def _asset_map(self) -> Mapping[str, dict]:
        album_id = self._album["id"]

        def load() -> tuple[dict[str, dict], Any]:
            signature = self._client.get_album_signature(self._api_key, album_id)
            return self._build_map(album_id), signature

        def refresh(previous: Any) -> tuple[dict[str, dict], Any] | None:
            signature = self._client.get_album_signature(self._api_key, album_id)
            if signature == previous:
                return None
            return self._build_map(album_id), signature

        # Stale-while-revalidate: a browse never waits for the album to be
        # re-fetched from Immich. Only the album's cheap (assetCount,
        # updatedAt) signature is checked in the background; the full
        # paginated asset list + size prefetch is rebuilt only when that
        # signature actually moved. A just-added asset shows up a poll or
        # two later -- clients poll continuously anyway.
        try:
            return self._asset_cache.get(
                (self._api_key, album_id),
                load=load,
                refresh=refresh,
                label=f"album {album_id}",
                evict_on=ImmichAuthError,
            )
        except ImmichAuthError as exc:
            raise _auth_challenge(exc) from exc

    def _build_map(self, album_id: str) -> dict[str, dict]:
        assets = self._client.list_album_assets(self._api_key, album_id)
        used: set[str] = set()
        result: dict[str, dict] = {}
        for asset in assets:
            asset_id = asset.get("id")
            if not isinstance(asset_id, str) or not asset_id:
                continue

            stem = _strip_extension(str(asset.get("originalFileName") or "Untitled Asset"))
            ext = output_extension(asset.get("originalMimeType"))
            name = _unique_name(f"{stem}{ext}", used, asset_id)
            result[name] = asset

        self._prefetch_sizes(result.values())
        return result

    def _prefetch_sizes(self, assets) -> None:
        """Resolve every not-yet-known asset's exact byte size, so the PROPFIND
        that triggered this listing can report a real `getcontentlength`.

        Clients like `rclone --vfs-cache-mode full` size their download from the
        listing and treat a missing/zero length as an empty file. Learning it
        lazily on the first GET is too late. Probes run concurrently
        (SIZE_PROBE_CONCURRENCY) and the result is cached for weeks, so this is
        a one-time cost per album per SIZE_CACHE_TTL_SECONDS window.
        """
        specs = [
            (asset["id"], not is_web_supported(asset.get("originalMimeType")))
            for asset in assets
            if isinstance(asset.get("id"), str)
            and not isinstance(self._size_cache.get(asset["id"]), int)
        ]
        learned = {
            asset_id: size
            for asset_id, size in self._client.get_asset_sizes(self._api_key, specs).items()
            if isinstance(size, int) and size > 0
        }
        self._size_cache.set_many(learned)

    def get_member_names(self) -> list[str]:
        return sorted(self._asset_map())

    def get_member(self, name: str):
        asset = self._asset_map().get(name)
        if asset is None:
            return None
        return ImmichAsset(
            join_uri(self.path, name),
            self.environ,
            self._client,
            self._size_cache,
            self._api_key,
            asset,
        )


def _discard(raw, count: int) -> None:
    """Read and throw away `count` bytes -- used when Immich ignores a Range."""
    remaining = count
    while remaining > 0:
        chunk = raw.read(min(remaining, 1 << 16))
        if not chunk:
            return
        remaining -= len(chunk)


class _ImmichAssetStream:
    """Seekable, lazy byte stream over one Immich asset, backed by ranged GETs.

    wsgidav's GET handler drives this as `seek(range_start)` (once, to the
    start of the requested range -- 0 for a whole-file GET) then repeated
    `read(n)` then `close()`. Nothing is fetched until the first `read()`;
    that opens the Immich request with a `Range: bytes=<pos>-` header (or no
    Range at all when starting from 0), so a range request only transfers the
    tail that was actually asked for. A `seek()` to a different offset drops
    any in-flight response so the next `read()` reopens at the new position.
    """

    def __init__(
        self,
        client: ImmichClient,
        api_key: str,
        asset_id: str,
        *,
        use_fullsize: bool,
        size_cache: TTLCache,
    ) -> None:
        self._client = client
        self._api_key = api_key
        self._asset_id = asset_id
        self._use_fullsize = use_fullsize
        self._size_cache = size_cache
        self._pos = 0
        self._response = None
        self._raw = None

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence != 0:
            raise ValueError("_ImmichAssetStream only supports absolute seeks")
        if offset != self._pos:
            self._close_response()
            self._pos = offset
        return self._pos

    def read(self, size: int = -1) -> bytes:
        if self._raw is None:
            self._open()
        data = self._raw.read(size) if size is not None and size >= 0 else self._raw.read()
        self._pos += len(data)
        return data

    def _open(self) -> None:
        # An ImmichAuthError here (key revoked mid-download, with the size
        # already cached so get_content_length didn't probe) propagates after
        # the 200 headers were sent -- it can't become a clean 401, the
        # transfer just fails. Rare; the common auth-failure paths (listing,
        # first GET) all surface a proper 401 earlier.
        range_header = f"bytes={self._pos}-" if self._pos else None
        self._response = self._client.fetch_asset_content(
            self._api_key,
            self._asset_id,
            use_fullsize=self._use_fullsize,
            range_header=range_header,
        )

        total = content_total_size(self._response)
        if total is not None:
            cached = self._size_cache.get(self._asset_id)
            if isinstance(cached, int) and cached != total:
                logger.warning(
                    "Asset %s size changed since it was probed (%d -> %d) -- "
                    "refreshing the cache",
                    self._asset_id, cached, total,
                )
            self._size_cache.set(self._asset_id, total)

        if range_header and self._response.status_code == 200:
            # Immich didn't honour the Range and sent the whole body from 0.
            _discard(self._response.raw, self._pos)

        self._raw = self._response.raw

    def _close_response(self) -> None:
        if self._response is not None:
            self._response.close()
        self._response = None
        self._raw = None

    def close(self) -> None:
        self._close_response()


class ImmichAsset(DAVNonCollection):
    """Read-only WebDAV file backed by a live Immich asset fetch.

    Content comes from `?size=fullsize` for anything Immich itself doesn't
    consider web-supported (RAW, HEIC/HEIF, etc.), or from the asset's
    original bytes otherwise -- see formats.py for exactly which is which,
    and why that boundary has to match Immich's own classification exactly.

    Byte size is never guessed from metadata (`exifInfo.fileSizeInByte` is
    not the number Immich actually streams). It's learned only from a real
    response's authoritative length -- `Content-Range` on a ranged reply or
    `Content-Length` on a whole-body one -- and only cached when it's a
    positive integer, so a degenerate `Content-Length: 0` can't poison the
    cache for SIZE_CACHE_TTL_SECONDS. AlbumCollection probes every asset's
    size when it builds a listing (see _prefetch_sizes), so a PROPFIND
    normally reports a real length straight from cache; only a straggler
    added since that listing falls back to an inline one-byte range probe on
    its first GET/HEAD. Range requests are proxied through to Immich (see
    _ImmichAssetStream), which is what lets clients like
    `rclone --vfs-cache-mode full` and chunked downloaders work correctly.
    """

    def __init__(
        self,
        path: str,
        environ: dict[str, Any],
        client: ImmichClient,
        size_cache: TTLCache,
        api_key: str,
        asset: dict,
    ) -> None:
        super().__init__(path, environ)
        self._client = client
        self._size_cache = size_cache
        self._api_key = api_key
        self._asset = asset
        # get_content_length() is called several times per request (directly
        # and via support_content_length()); resolve the probe at most once.
        self._length_resolved = False
        self._length: int | None = None

    def _use_fullsize(self) -> bool:
        return not is_web_supported(self._asset.get("originalMimeType"))

    def get_content_type(self) -> str:
        if self._use_fullsize():
            return FULLSIZE_CONTENT_TYPE
        mimetype = self._asset.get("originalMimeType")
        return mimetype if isinstance(mimetype, str) else "application/octet-stream"

    def support_ranges(self) -> bool:
        return True

    def get_content_length(self) -> int | None:
        if not self._length_resolved:
            self._length = self._resolve_length()
            self._length_resolved = True
        return self._length

    def _resolve_length(self) -> int | None:
        asset_id = self._asset["id"]

        cached = self._size_cache.get(asset_id)
        if isinstance(cached, int) and cached > 0:
            return cached

        if self.environ.get("REQUEST_METHOD") not in ("GET", "HEAD"):
            # A PROPFIND. The size is normally already cached -- AlbumCollection
            # probes every asset when it builds the listing. This branch only
            # hits a straggler added between that listing and this request;
            # report unknown rather than block the PROPFIND on a probe.
            return None

        # A GET/HEAD with no cached size (straggler, or the listing probe
        # failed). wsgidav needs the length up front to satisfy a Range
        # request. Learn it with a one-byte probe. A genuinely failed fetch
        # (including an empty body -- see fetch_asset_content) raises
        # ImmichError, left to propagate: wsgidav turns it into a clean 500
        # before any response headers are sent, and nothing is cached.
        try:
            probe = self._client.fetch_asset_content(
                self._api_key, asset_id, use_fullsize=self._use_fullsize(),
                range_header="bytes=0-0",
            )
        except ImmichAuthError as exc:
            raise _auth_challenge(exc) from exc
        try:
            total = content_total_size(probe)
        finally:
            probe.close()

        if total is None:
            # 2xx with no Content-Range and no Content-Length (chunked) --
            # the fetch is fine, we just can't state the size up front.
            logger.warning("Could not determine a byte size for asset %s", asset_id)
            return None

        self._size_cache.set(asset_id, total)
        return total

    def get_display_name(self) -> str:
        return self.name

    def get_etag(self) -> str | None:
        checksum = self._asset.get("checksum")
        return checksum if isinstance(checksum, str) else None

    def support_etag(self) -> bool:
        return isinstance(self._asset.get("checksum"), str)

    def get_creation_date(self) -> int | None:
        return _parse_timestamp(self._asset.get("fileCreatedAt"))

    def get_last_modified(self) -> int | None:
        return _parse_timestamp(self._asset.get("fileModifiedAt"))

    def get_content(self):
        return _ImmichAssetStream(
            self._client,
            self._api_key,
            self._asset["id"],
            use_fullsize=self._use_fullsize(),
            size_cache=self._size_cache,
        )
