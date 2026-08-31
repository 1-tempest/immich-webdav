from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable, List, TypeVar

T = TypeVar("T")
R = TypeVar("R")


def concurrent_map(fn: Callable[[T], R], items: Iterable[T], max_workers: int) -> List[R]:
    """Run fn over items concurrently, bounded by max_workers.

    Used where a single logical operation needs results from several
    independent Immich calls -- e.g. fetching more than one page of a large
    album's asset list -- to cut wall-clock latency versus fetching them one
    at a time. Results are returned in the same order as `items`.

    Not used for the common case (one call is enough); only worth reaching
    for when there's genuinely more than one independent request to make.
    """
    items = list(items)
    if len(items) <= 1:
        return [fn(item) for item in items]

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        return list(executor.map(fn, items))
