"""TensorRT optimisation: chosen only on NVIDIA + tensorrt, cached, safe fallback."""

from __future__ import annotations

from pathlib import Path

import storeguard.detector as detector
from storeguard import optimize as opt


def test_only_cuda_with_tensorrt_gets_an_engine(monkeypatch) -> None:
    monkeypatch.setattr(opt, "has_tensorrt", lambda: True)
    monkeypatch.setattr(opt, "_gpu_tag", lambda: "nvidia-geforce-rtx-3060")
    assert opt.target_format("cuda") == "engine"
    assert opt.target_format("mps") is None and opt.target_format("cpu") is None
    assert opt.cached_path("yolo26m.pt", "cuda", 640) == Path(
        "models/optimized/yolo26m-640-nvidia-geforce-rtx-3060.engine"
    )
    monkeypatch.setattr(opt, "has_tensorrt", lambda: False)
    assert opt.target_format("cuda") is None and opt.cached_path("yolo26m.pt", "cuda", 640) is None


def test_failed_export_falls_back(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(opt, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(opt, "has_tensorrt", lambda: True)
    monkeypatch.setattr(opt, "_gpu_tag", lambda: "gpu")

    def boom(*a, **k):
        raise RuntimeError("TensorRT build failed")

    monkeypatch.setattr(opt, "_export", boom)
    assert opt.build("yolo26m.pt", "cuda", 640) is None


def test_cached_build_is_reused(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(opt, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(opt, "has_tensorrt", lambda: True)
    monkeypatch.setattr(opt, "_gpu_tag", lambda: "gpu")
    calls = []
    monkeypatch.setattr(opt, "_export", lambda w, d, i, dest: calls.append(dest) or dest.write_bytes(b"e") or dest)
    monkeypatch.setattr(opt, "_check", lambda *a: None)
    first = opt.build("yolo26m.pt", "cuda", 640)
    assert first is not None and first.exists()
    assert opt.build("yolo26m.pt", "cuda", 640) == first and len(calls) == 1


class _FakeYOLO:
    def __init__(self, path, task=None):
        self.path = path


def test_detection_engine_uses_cached_engine(monkeypatch, tmp_path) -> None:
    engine_file = tmp_path / "yolo26m-640-gpu.engine"
    engine_file.write_bytes(b"e")
    monkeypatch.setattr(detector, "YOLO", _FakeYOLO)
    monkeypatch.setattr(opt, "cached_path", lambda w, d, i: engine_file)
    monkeypatch.setattr(opt, "_check", lambda *a: None)  # the engine runs
    eng = detector._DetectionEngine("yolo26m.pt", "cuda", 640, optimize="auto")
    assert eng.runtime == "tensorrt" and eng.model.path == str(engine_file)
    plain = detector._DetectionEngine("yolo26m.pt", "cuda", 640, optimize="off")
    assert plain.runtime == "pytorch" and plain.model.path == "yolo26m.pt"


def test_detection_engine_builds_in_background_then_swaps(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(detector, "YOLO", _FakeYOLO)
    target = tmp_path / "x.engine"
    monkeypatch.setattr(opt, "cached_path", lambda w, d, i: target)
    started = {}
    monkeypatch.setattr(opt, "build_in_background", lambda w, d, i, cb: started.setdefault("cb", cb))
    eng = detector._DetectionEngine("yolo26m.pt", "cuda", 640, optimize="auto")
    assert eng.runtime == "pytorch"  # keeps detecting while the engine builds
    started["cb"](target)
    assert eng.runtime == "tensorrt" and eng.model.path == str(target)


def test_stale_cached_engine_is_rebuilt(monkeypatch, tmp_path) -> None:
    engine_file = tmp_path / "old.engine"
    engine_file.write_bytes(b"e")
    monkeypatch.setattr(detector, "YOLO", _FakeYOLO)
    monkeypatch.setattr(opt, "cached_path", lambda w, d, i: engine_file)

    def broken(*a):
        raise RuntimeError("engine built for another TensorRT version")

    monkeypatch.setattr(opt, "_check", broken)
    rebuilt = []
    monkeypatch.setattr(opt, "build_in_background", lambda *a: rebuilt.append(a))
    eng = detector._DetectionEngine("yolo26m.pt", "cuda", 640, optimize="auto")
    assert eng.runtime == "pytorch" and not engine_file.exists() and len(rebuilt) == 1


def test_tensorrt_failure_falls_back_to_pytorch(monkeypatch) -> None:
    class _Model:
        def __init__(self, path, task=None):
            self.path = path

        def predict(self, frames, **kw):
            if str(self.path).endswith(".engine"):
                raise RuntimeError("CUDA error")
            return [type("R", (), {"boxes": None})() for _ in frames]

    import numpy as np

    monkeypatch.setattr(detector, "YOLO", _Model)
    eng = detector._DetectionEngine("yolo26m.pt", "cuda", 640, optimize="off")
    eng.model, eng.runtime = _Model("x.engine"), "tensorrt"
    out = eng._run([(np.zeros((10, 10, 3), np.uint8), (0,), 0.5)])
    assert eng.runtime == "pytorch" and out[0].shape == (0, 6)
