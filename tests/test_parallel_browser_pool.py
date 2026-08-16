"""Regression test for isolated-browser assignment in `aitlc parallel run`.

The bug: browsers were assigned by `per_job_cdp[index % jobs]`. With more
features than jobs, ThreadPoolExecutor starts task N as soon as *any*
worker frees, so task `jobs` (-> index 0) could claim the browser task 0
was still driving. Two concurrent runs then shared one Chrome — exactly the
interleaving `--isolated` exists to prevent.

This models the scheduler rather than launching browsers: the invariant is
about who holds which URL *at the same time*, which is testable without a
real Chrome.
"""

from __future__ import annotations

import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor


def _run_with_index_assignment(n_tasks: int, jobs: int, durations: list[float]):
    """Model the OLD, buggy scheme: browser chosen by task index."""
    urls = [f"url-{i}" for i in range(jobs)]
    in_use: dict[str, int] = {}
    lock = threading.Lock()
    collisions: list[tuple[int, str]] = []

    def work(index: int) -> None:
        url = urls[index % len(urls)]
        with lock:
            if url in in_use:
                collisions.append((index, url))
            in_use[url] = index
        time.sleep(durations[index])
        with lock:
            if in_use.get(url) == index:
                del in_use[url]

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        list(pool.map(work, range(n_tasks)))
    return collisions


def _run_with_checkout_pool(n_tasks: int, jobs: int, durations: list[float]):
    """Model the FIXED scheme: browsers are checked out of a queue."""
    pool_q: queue.Queue[str] = queue.Queue()
    for i in range(jobs):
        pool_q.put(f"url-{i}")

    in_use: dict[str, int] = {}
    lock = threading.Lock()
    collisions: list[tuple[int, str]] = []

    def work(index: int) -> None:
        url = pool_q.get()
        try:
            with lock:
                if url in in_use:
                    collisions.append((index, url))
                in_use[url] = index
            time.sleep(durations[index])
            with lock:
                if in_use.get(url) == index:
                    del in_use[url]
        finally:
            pool_q.put(url)

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        list(pool.map(work, range(n_tasks)))
    return collisions


# Task 0 runs long; tasks 1..2 finish fast, freeing workers so task 3
# (index 3 -> 3 % 3 == 0) starts while task 0 is still running.
_DURATIONS = [0.60, 0.01, 0.01, 0.01, 0.01, 0.01]


class TestBrowserAssignment:
    def test_index_assignment_collides(self):
        # Guards the guard: if this ever stops colliding, the fixed-scheme
        # test below would be passing for the wrong reason.
        collisions = _run_with_index_assignment(6, 3, _DURATIONS)
        assert collisions, "expected the old index scheme to double-book a browser"

    def test_checkout_pool_never_double_books(self):
        collisions = _run_with_checkout_pool(6, 3, _DURATIONS)
        assert collisions == [], f"browser double-booked: {collisions}"

    def test_checkout_pool_holds_under_many_tasks(self):
        durations = [0.3] + [0.01] * 19
        collisions = _run_with_checkout_pool(20, 4, durations)
        assert collisions == []

    def test_browser_returned_even_when_task_raises(self):
        """A failing run must return its browser or the pool shrinks to a deadlock."""
        pool_q: queue.Queue[str] = queue.Queue()
        pool_q.put("only-url")

        def work(index: int) -> None:
            url = pool_q.get()
            try:
                raise RuntimeError("boom")
            finally:
                pool_q.put(url)

        with ThreadPoolExecutor(max_workers=1) as pool:
            for future in [pool.submit(work, i) for i in range(3)]:
                try:
                    future.result()
                except RuntimeError:
                    pass

        assert pool_q.qsize() == 1, "browser was not returned after a failure"
