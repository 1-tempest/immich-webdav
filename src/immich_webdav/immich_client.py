from __future__ import annotations

import logging
import time
from typing import Any

import requests

from immich_webdav.concurrency import concurrent_map

logger = logging.getLogger(__name__)

JsonObject = dict[str, Any]


class ImmichError(RuntimeError):
    """Raised for any Immich request failure."""


class ImmichAuthError(ImmichError):
    """Immich rejected the API key (HTTP 401/403).

    Distinct from a generic failure so the WebDAV layer can turn it into a
    `401` credential challenge instead of a `500`. There is no separate
    up-front key check -- every real Immich call already carries the key, so
    a bad key surfaces here on the first one.
    """


def content_total_size(response) -> int | None:
    """The asset's full byte length, read from whichever header Immich set.

    A ranged (`206`) reply carries `Content-Range: bytes <s>-<e>/<total>`; a
    plain `200` carries `Content-Length` for the whole body. Returns None if
    neither yields a usable positive integer.
    """
    content_range = response.headers.get("Content-Range")
    if content_range and "/" in content_range:
        tail = content_range.rsplit("/", 1)[-1].strip()
        if tail.isdigit():
            value = int(tail)
            return value if value > 0 else None

    if response.status_code == 200:
        content_length = response.headers.get("Content-Length")
        if content_length and content_length.strip().isdigit():
            value = int(content_length.strip())
            return value if value > 0 else None

    return None


class ImmichClient:
    """Thin Immich API client.

    Deliberately stateless with respect to identity: this process serves
    many different users concurrently, each with their own API key, so
    every call takes the key explicitly rather than baking one into a
    shared session.
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float,
        max_retries: int,
        page_size: int,
        max_concurrent_requests: int,
        size_probe_concurrency: int,
    ) -> None:
        self._base_url = base_url
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._page_size = page_size
        self._max_concurrent_requests = max_concurrent_requests
        self._size_probe_concurrency = size_probe_concurrency

    def list_albums(self, api_key: str) -> list[JsonObject]:
        payload = self._request_json("GET", "/api/albums", api_key)
        if not isinstance(payload, list):
            raise ImmichError("GET /api/albums returned a non-list response")
        return [a for a in payload if isinstance(a, dict)]

    def get_album_signature(self, api_key: str, album_id: str) -> tuple[Any, ...]:
        """A cheap fingerprint of an album's current state, for staleness checks.

        `GET /api/albums/{id}?withoutAssets=true` returns the album's metadata
        without paging through every asset. This tuple moves whenever assets
        are added or removed (`assetCount`), the album itself is touched
        (`updatedAt`), or any member asset is modified
        (`lastModifiedAssetTimestamp`, when the Immich version reports it) --
        so comparing it is far cheaper than re-fetching and diffing the whole
        asset list.
        """
        payload = self._request_json(
            "GET", f"/api/albums/{album_id}?withoutAssets=true", api_key
        )
        if not isinstance(payload, dict):
            raise ImmichError(f"GET /api/albums/{album_id} returned a non-object response")
        return (
            payload.get("assetCount"),
            payload.get("updatedAt"),
            payload.get("lastModifiedAssetTimestamp"),
        )

    def list_album_assets(self, api_key: str, album_id: str) -> list[JsonObject]:
        first_page = self._search_page(api_key, album_id, page=1)
        items = list(first_page["items"])

        next_page = first_page["nextPage"]
        page_was_full = first_page["count"] >= self._page_size

        if next_page is None or not page_was_full:
            return items

        # Pages in this API are just sequential integers, so once we know
        # there's more than one, we can speculatively fetch several page
        # numbers at once instead of waiting on each `nextPage` in turn.
        page_number = int(next_page)
        while True:
            batch = list(range(page_number, page_number + self._max_concurrent_requests))
            results = concurrent_map(
                lambda page: self._search_page(api_key, album_id, page=page),
                batch,
                self._max_concurrent_requests,
            )

            reached_end = False
            for result in results:
                items.extend(result["items"])
                if result["count"] < self._page_size or result["nextPage"] is None:
                    reached_end = True

            if reached_end:
                break
            page_number += self._max_concurrent_requests

        return items

    def fetch_asset_content(
        self,
        api_key: str,
        asset_id: str,
        *,
        use_fullsize: bool,
        range_header: str | None = None,
    ):
        """Returns a streaming `requests.Response` for the asset's bytes.

        When `range_header` is given (a raw HTTP `Range` value like
        `bytes=0-1023`) it's forwarded to Immich, and a `206 Partial Content`
        reply is accepted alongside `200`. Immich may still answer `200` with
        the whole body if it doesn't honour the range -- the caller must cope.

        A `2xx` reply whose declared `Content-Length` is `<= 0` is rejected
        here as a failed fetch, not returned. Immich has been observed to
        answer `200` with an empty body for an asset with no generated
        derivative (or transiently); letting that through would serve a
        silent 0-byte file and -- worse -- get that 0 cached as the asset's
        size for `SIZE_CACHE_TTL_SECONDS`. Failing loudly at this single
        choke point means both `get_content_length()` and `get_content()`
        are covered, nothing poisoned is cached, and the next access retries
        for real.

        Caller owns closing it. Deliberately not routed through
        `_request_json`'s retry loop -- retrying a partially-streamed body
        transparently isn't safe, so a content fetch failure surfaces
        directly rather than being silently retried.
        """
        if use_fullsize:
            url = f"{self._base_url}/api/assets/{asset_id}/thumbnail"
            params: dict[str, str] = {"size": "fullsize"}
        else:
            url = f"{self._base_url}/api/assets/{asset_id}/original"
            params = {}

        headers = {"x-api-key": api_key}
        if range_header:
            headers["Range"] = range_header

        response = requests.get(
            url,
            params=params,
            headers=headers,
            timeout=self._timeout,
            stream=True,
        )
        if response.status_code not in (200, 206):
            status = response.status_code
            response.close()
            if status in (401, 403):
                raise ImmichAuthError(f"Immich rejected the API key (HTTP {status})")
            raise ImmichError(
                f"Immich returned HTTP {status} fetching asset {asset_id}"
            )

        declared = response.headers.get("Content-Length")
        if declared is not None:
            try:
                declared_length = int(declared)
            except ValueError:
                declared_length = None
            if declared_length is not None and declared_length <= 0:
                response.close()
                raise ImmichError(
                    f"Immich returned HTTP {response.status_code} with "
                    f"Content-Length {declared!r} for asset {asset_id} -- "
                    "treating as a failed fetch"
                )

        response.raw.decode_content = True
        return response

    def get_asset_size(
        self, api_key: str, asset_id: str, *, use_fullsize: bool
    ) -> int | None:
        """The exact byte length Immich will stream for this asset.

        A one-byte `Range: bytes=0-0` probe against the same URL
        `fetch_asset_content` uses, so the total it reads from `Content-Range`
        is guaranteed to match a later full GET's `Content-Length`. Returns
        None if it can't be determined or the fetch fails (e.g. Immich has no
        content for the asset yet) -- the caller must not let one bad asset
        break a whole listing; a real GET of it will surface the failure.
        """
        try:
            response = self.fetch_asset_content(
                api_key, asset_id, use_fullsize=use_fullsize, range_header="bytes=0-0"
            )
        except (ImmichError, requests.RequestException) as exc:
            # One flaky probe must not fail the whole album listing -- report
            # unknown for this asset; its own GET will still surface a real
            # problem. (A transient failure just gets re-probed next listing.)
            logger.warning("Size probe failed for asset %s: %s", asset_id, exc)
            return None
        try:
            return content_total_size(response)
        finally:
            response.close()

    def get_asset_sizes(
        self, api_key: str, specs: list[tuple[str, bool]]
    ) -> dict[str, int | None]:
        """Probe many asset sizes concurrently. `specs` is (asset_id, use_fullsize).

        `SIZE_PROBE_CONCURRENCY <= 0` disables listing-time probing entirely --
        sizes then fall back to being learned on each asset's first GET (and a
        PROPFIND for a not-yet-fetched asset reports unknown).
        """
        if not specs or self._size_probe_concurrency <= 0:
            return {}
        results = concurrent_map(
            lambda spec: (spec[0], self.get_asset_size(
                api_key, spec[0], use_fullsize=spec[1]
            )),
            specs,
            self._size_probe_concurrency,
        )
        return dict(results)

    def _search_page(self, api_key: str, album_id: str, *, page: int) -> JsonObject:
        payload = self._request_json(
            "POST",
            "/api/search/metadata",
            api_key,
            json_body={
                "albumIds": [album_id],
                "type": "IMAGE",
                "page": page,
                "size": self._page_size,
            },
        )
        assets = payload.get("assets") if isinstance(payload, dict) else None
        if not isinstance(assets, dict):
            raise ImmichError("Immich search response missing an assets object")

        items = assets.get("items")
        if not isinstance(items, list):
            raise ImmichError("Immich search response has invalid assets.items")

        return {
            "items": [i for i in items if isinstance(i, dict)],
            "count": assets.get("count", len(items)),
            "nextPage": assets.get("nextPage"),
        }

    def _request_json(
        self,
        method: str,
        path: str,
        api_key: str,
        *,
        json_body: JsonObject | None = None,
    ) -> Any:
        url = f"{self._base_url}{path}"
        headers = {"x-api-key": api_key, "Accept": "application/json"}

        for attempt in range(1, self._max_retries + 1):
            try:
                response = requests.request(
                    method, url, json=json_body, headers=headers, timeout=self._timeout
                )
            except requests.RequestException as exc:
                if attempt >= self._max_retries:
                    raise ImmichError(
                        f"{method} {url} failed after {attempt} attempts"
                    ) from exc
                logger.warning(
                    "Immich request error for %s %s (attempt %d/%d): %s",
                    method, url, attempt, self._max_retries, exc,
                )
                time.sleep(min(2 ** (attempt - 1), 4))
                continue

            if response.ok:
                try:
                    return response.json()
                except ValueError as exc:
                    raise ImmichError(f"{method} {url} returned invalid JSON") from exc

            if response.status_code in (401, 403):
                raise ImmichAuthError(
                    f"Immich rejected the API key (HTTP {response.status_code} for {method} {path})"
                )

            retryable = response.status_code in {408, 429} or response.status_code >= 500
            if not retryable or attempt >= self._max_retries:
                raise ImmichError(f"{method} {url} returned HTTP {response.status_code}")

            logger.warning(
                "Immich returned HTTP %d for %s %s (attempt %d/%d)",
                response.status_code, method, url, attempt, self._max_retries,
            )
            time.sleep(min(2 ** (attempt - 1), 4))

        raise AssertionError("unreachable")
