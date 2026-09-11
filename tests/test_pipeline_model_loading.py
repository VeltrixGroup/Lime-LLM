"""_build_tracker must not serialize camera connects once weights are cached.

Regression test: connecting N cameras at once used to queue every session's
full model construction behind one lock, so the Nth camera's stream didn't
even start opening until N model loads had happened one after another. Only
the very first load (which might need to download the weights) should be
exclusive — everything after that should build in parallel.
"""

from __future__ import annotations

import threading
import time

import storeguard.dashboard.pipeline as pipeline
from storeguard.config import DetectorCfg
from storeguard.dashboard.pipeline import _build_tracker

_BUILD_DELAY = 0.15


class _SlowTracker:
    def __init__(self, cfg) -> None:
        time.sleep(_BUILD_DELAY)


def test_concurrent_builds_parallelize_after_first_load(monkeypatch) -> None:
    monkeypatch.setattr(pipeline, "PersonTracker", _SlowTracker)
    monkeypatch.setattr(pipeline, "_weights_loaded", set())

    cfg = DetectorCfg()
    _build_tracker(cfg)  # prime the cache with a legitimately-serialized first load

    start = time.monotonic()
    threads = [threading.Thread(target=_build_tracker, args=(cfg,)) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - start

    # Fully serialized would take 3 x _BUILD_DELAY (0.45s+); parallel finishes
    # in roughly one _BUILD_DELAY plus scheduling slack.
    assert elapsed < _BUILD_DELAY * 2, f"expected parallel construction, took {elapsed:.2f}s"


def test_first_load_for_a_path_is_still_exclusive(monkeypatch) -> None:
    monkeypatch.setattr(pipeline, "_weights_loaded", set())

    start_times: list[float] = []
    record_lock = threading.Lock()

    class _RecordingSlowTracker:
        def __init__(self, cfg) -> None:
            with record_lock:
                start_times.append(time.monotonic())
            time.sleep(_BUILD_DELAY)

    monkeypatch.setattr(pipeline, "PersonTracker", _RecordingSlowTracker)

    cfg = DetectorCfg()
    t0 = time.monotonic()
    threads = [threading.Thread(target=_build_tracker, args=(cfg,)) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Three threads race for a never-loaded path: exactly one construction
    # should start immediately (the exclusive first load) — the other two
    # must wait behind the lock for it to finish before starting their own.
    early = [s for s in start_times if s - t0 < _BUILD_DELAY / 2]
    assert len(early) == 1, f"expected exactly 1 immediate start, got {len(early)}: {start_times}"
