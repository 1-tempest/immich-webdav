from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Callable, Generic, Hashable, TypeVar

T = TypeVar("T")

logger = logging.getLogger(__name__)


class TTLCache(Generic[T]):
    """Minimal thread-safe TTL cache.

    Deliberately dumb: no eviction thread, no LRU, no "retain the previous
    value on a failed refresh" logic. Used at two very different TTLs in
    this project -- a few-second album-list cache and a ~5 minute auth cache
    (via `get_or_compute` / `get` / `set`) and a ~2 week asset-size cache
    (via `get` / `set` / `set_many`, since a served file's byte size
    essentially never changes). Album *asset* lists use `SwrCache` instead.

    Also deliberately doesn't dedupe concurrent in-flight computations for
    the same key. Two requests landing in the same instant near expiry can
    both trigger a fetch. At the scale this is built for (a handful of
    users, an Immich instance on the same LAN), that's not worth the extra
    locking complexity a proper single-flight implementation would add.

    Optional persistence: pass `persist_path` and the cache is loaded from
    that JSON file on start and rewritten (atomically) whenever an entry's
    value changes. Only meaningful for the asset-size cache -- it lets a
    container restart skip re-probing every asset's byte size. A persistent
    cache uses wall-clock time so expiry survives the restart; a
    non-persistent one uses a monotonic clock (immune to clock changes).
    Keys must be JSON-string-serialisable when persistence is on.
    """

    def __init__(self, ttl_seconds: float, *, persist_path: str | None = None) -> None:
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._store: dict[Hashable, tuple[float, T]] = {}
        self._persist_path = persist_path
        self._time: Callable[[], float] = time.time if persist_path else time.monotonic
        self._dirty = False
        if persist_path:
            self._load()

    def get_or_compute(self, key: Hashable, compute: Callable[[], T]) -> T:
        now = self._time()
        with self._lock:
            cached = self._store.get(key)
            if cached is not None and (now - cached[0]) < self._ttl:
                return cached[1]

        value = compute()

        with self._lock:
            self._store[key] = (now, value)
        return value

    def get(self, key: Hashable) -> T | None:
        """Return the cached value if present and unexpired, else None.

        Never computes -- for callers that want a cheap "do we already
        know this" check without paying for a fetch on a miss.
        """
        now = self._time()
        with self._lock:
            cached = self._store.get(key)
            if cached is not None and (now - cached[0]) < self._ttl:
                return cached[1]
        return None

    def set(self, key: Hashable, value: T) -> None:
        """Store a value, e.g. one learned as a side effect of other work."""
        with self._lock:
            existing = self._store.get(key)
            self._store[key] = (self._time(), value)
            if existing is None or existing[1] != value:
                self._dirty = True
        self._persist_if_dirty()

    def set_many(self, items: dict[Hashable, T]) -> None:
        """Store many values under one lock and at most one disk write."""
        if not items:
            return
        with self._lock:
            now = self._time()
            for key, value in items.items():
                existing = self._store.get(key)
                self._store[key] = (now, value)
                if existing is None or existing[1] != value:
                    self._dirty = True
        self._persist_if_dirty()

    def flush(self) -> None:
        """Write any pending changes to disk (no-op without `persist_path`)."""
        self._persist_if_dirty()

    # -- persistence ------------------------------------------------------

    def _load(self) -> None:
        try:
            with open(self._persist_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            logger.warning("Ignoring unreadable cache file %s: %s", self._persist_path, exc)
            return

        entries = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(entries, dict):
            return

        now = self._time()
        for key, pair in entries.items():
            if not isinstance(pair, list) or len(pair) != 2:
                continue
            timestamp, value = pair
            if isinstance(timestamp, (int, float)) and (now - timestamp) < self._ttl:
                self._store[key] = (float(timestamp), value)
        logger.info("Loaded %d cache entries from %s", len(self._store), self._persist_path)

    def _persist_if_dirty(self) -> None:
        if not self._persist_path:
            return
        with self._lock:
            if not self._dirty:
                return
            now = self._time()
            snapshot = {
                str(key): [timestamp, value]
                for key, (timestamp, value) in self._store.items()
                if (now - timestamp) < self._ttl
            }
            self._dirty = False

        payload = {"version": 1, "entries": snapshot}
        tmp_path = f"{self._persist_path}.tmp"
        try:
            parent = os.path.dirname(self._persist_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(tmp_path, self._persist_path)
        except OSError as exc:
            logger.warning("Could not persist cache to %s: %s", self._persist_path, exc)
            with self._lock:
                self._dirty = True


class _SwrEntry(Generic[T]):
    __slots__ = ("value", "token", "checked_at")

    def __init__(self, value: T, token: Any, checked_at: float) -> None:
        self.value = value
        self.token = token
        self.checked_at = checked_at


class SwrCache(Generic[T]):
    """Stale-while-revalidate cache.

    `get()` returns the last known value immediately -- even when stale -- and
    triggers a background refresh once the value is older than
    `recheck_seconds`. Callers never block on a refresh once anything is
    cached; the updated value lands on a later `get()`. WebDAV clients
    (Windows, rclone) poll directories continuously, so in practice a
    listing is at most a few seconds behind rather than "hangs while the
    whole album is re-fetched from Immich".

    Only the first `get()` for a key (nothing cached yet) blocks, on `load`.

    `load()` returns `(value, token)`. `refresh(token)` returns `(value,
    token)` to replace the entry, or `None` if `token` shows nothing
    changed -- letting the caller make the common "did this change?" check
    cheap and only pay for a full rebuild when it actually did.

    Not persisted -- directory listings are cheap enough to rebuild once on
    start, and must reflect reality, not a two-week-old snapshot.
    """

    def __init__(self, recheck_seconds: float) -> None:
        self._recheck = recheck_seconds
        self._lock = threading.Lock()
        self._entries: dict[Hashable, _SwrEntry[T]] = {}
        self._refreshing: set[Hashable] = set()

    def get(
        self,
        key: Hashable,
        *,
        load: Callable[[], tuple[T, Any]],
        refresh: Callable[[Any], tuple[T, Any] | None],
        label: str = "",
        evict_on: type[BaseException] | tuple[type[BaseException], ...] = (),
    ) -> T:
        """`label` is used only in log lines -- never pass a secret in `key`.

        If a background `refresh` raises an exception matching `evict_on`, the
        cached entry is dropped rather than kept -- so e.g. a now-invalid
        credential stops serving a stale listing and the next `get` blocks on
        `load` (which will raise too, letting the caller surface the error).
        """
        now = time.monotonic()
        value: T | None = None
        token: Any = None
        start_refresh = False

        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                value = entry.value
                token = entry.token
                if (now - entry.checked_at) >= self._recheck and key not in self._refreshing:
                    self._refreshing.add(key)
                    start_refresh = True

        if entry is not None:
            if start_refresh:
                threading.Thread(
                    target=self._run_refresh,
                    args=(key, token, refresh, label, evict_on),
                    name="swr-refresh",
                    daemon=True,
                ).start()
            return value  # type: ignore[return-value]

        loaded_value, loaded_token = load()
        with self._lock:
            self._entries[key] = _SwrEntry(loaded_value, loaded_token, time.monotonic())
        return loaded_value

    def _run_refresh(
        self,
        key: Hashable,
        token: Any,
        refresh: Callable[[Any], tuple[T, Any] | None],
        label: str,
        evict_on: type[BaseException] | tuple[type[BaseException], ...],
    ) -> None:
        result: tuple[T, Any] | None = None
        try:
            candidate = refresh(token)
            if candidate is None or (isinstance(candidate, tuple) and len(candidate) == 2):
                result = candidate
            else:
                logger.error("Ignoring malformed refresh result (%s)", label or "listing")
        except Exception as exc:  # noqa: BLE001 -- must never crash the process
            if evict_on and isinstance(exc, evict_on):
                logger.info("Dropping cached listing after refresh error (%s)", label or "listing")
                with self._lock:
                    self._refreshing.discard(key)
                    self._entries.pop(key, None)
                return
            logger.exception("Background listing refresh failed (%s)", label or "listing")

        with self._lock:
            self._refreshing.discard(key)
            entry = self._entries.get(key)
            if entry is not None:
                if result is not None:
                    entry.value, entry.token = result
                    logger.info(
                        "Listing changed, refreshed in background (%s)", label or "listing"
                    )
                entry.checked_at = time.monotonic()
