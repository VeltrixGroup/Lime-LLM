"""The shared detector rests between batches when several cameras share it.

With many cameras the detector ran back to back, the GPU at 100% — and the
GPU also draws the browser that shows the control panel. A lone camera must
never wait, though.
"""

from __future__ import annotations

import threading
import time

from storeguard.detector import _MAX_REST_SEC, _MicroBatcher


def test_single_frames_never_rest() -> None:
    batcher = _MicroBatcher(lambda items: items, name="t", max_batch=16, rest_share=0.25)
    started = time.monotonic()
    for i in range(50):
        assert batcher.submit(i) == i
    assert batcher.rest_sec == 0.0
    assert time.monotonic() - started < 1.0


def test_shared_batches_rest_for_a_share_of_their_run_time() -> None:
    sizes: list[int] = []

    def slow(items):
        sizes.append(len(items))
        time.sleep(0.04)
        return items

    batcher = _MicroBatcher(slow, name="t", max_batch=16, rest_share=0.25)
    results: list[int] = []

    def camera(n: int) -> None:
        for i in range(4):
            results.append(batcher.submit(n * 10 + i))

    threads = [threading.Thread(target=camera, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert sorted(results) == sorted(n * 10 + i for n in range(6) for i in range(4))
    assert max(sizes) > 1  # the cameras really shared batches
    shared_sec = sum(0.04 for size in sizes if size > 1)
    assert batcher.rest_sec > 0
    # about a quarter of the shared batches' time (with slack for timer jitter)
    assert 0.1 * shared_sec <= batcher.rest_sec <= 0.6 * shared_sec


def test_rest_is_capped(monkeypatch) -> None:
    def very_slow(items):
        time.sleep(0.45)
        return items

    batcher = _MicroBatcher(very_slow, name="t", max_batch=16, rest_share=0.25)
    gate = threading.Barrier(2)

    def camera() -> None:
        gate.wait()
        batcher.submit(1)
        batcher.submit(2)

    threads = [threading.Thread(target=camera) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    shared = 3  # at most: every batch after the first can hold both cameras
    assert batcher.rest_sec <= shared * _MAX_REST_SEC + 0.02


def test_rest_can_be_off() -> None:
    def slow(items):
        time.sleep(0.02)
        return items

    batcher = _MicroBatcher(slow, name="t", max_batch=16)
    threads = [threading.Thread(target=lambda: [batcher.submit(i) for i in range(3)]) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert batcher.rest_sec == 0.0
