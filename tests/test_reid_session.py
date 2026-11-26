"""The ReID model's ONNX Runtime session must not busy-wait between calls.

With ONNX Runtime's defaults (a thread per core, each spinning for more work)
the ReID model cost half a CPU core per camera while doing almost nothing.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

from storeguard.detector import _calm_onnx_session, _reid_threads


class _Options:
    def __init__(self) -> None:
        self.intra_op_num_threads = 0
        self.inter_op_num_threads = 0
        self.entries: dict[str, str] = {}

    def add_session_config_entry(self, key: str, value: str) -> None:
        self.entries[key] = value


def _fake_onnxruntime(created: list):
    def session(path, opts, providers):
        created.append((path, opts, providers))
        return SimpleNamespace(_model_path=path, calm=True)

    return SimpleNamespace(SessionOptions=_Options, InferenceSession=session)


def _reid(**backend) -> SimpleNamespace:
    return SimpleNamespace(model=SimpleNamespace(backend=SimpleNamespace(**backend)))


def test_session_is_rebuilt_with_a_small_quiet_thread_pool(monkeypatch) -> None:
    created: list = []
    monkeypatch.setitem(sys.modules, "onnxruntime", _fake_onnxruntime(created))
    reid = _reid(session=SimpleNamespace(_model_path="reid.onnx"), use_io_binding=False)

    assert _calm_onnx_session(reid) is True

    path, opts, providers = created[0]
    assert path == "reid.onnx"
    assert providers == ["CPUExecutionProvider"]
    assert 1 <= opts.intra_op_num_threads == _reid_threads() <= 4
    assert opts.entries["session.intra_op.allow_spinning"] == "0"
    assert reid.model.backend.session.calm is True


def test_unexpected_layout_keeps_the_original_session(monkeypatch) -> None:
    created: list = []
    monkeypatch.setitem(sys.modules, "onnxruntime", _fake_onnxruntime(created))

    no_session = _reid()
    assert _calm_onnx_session(no_session) is False

    original = SimpleNamespace(_model_path="reid.onnx")
    gpu_bound = _reid(session=original, use_io_binding=True)
    assert _calm_onnx_session(gpu_bound) is False
    assert gpu_bound.model.backend.session is original

    assert _calm_onnx_session(SimpleNamespace(model=None)) is False
    assert created == []


def test_a_failing_rebuild_is_not_fatal(monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("no such provider")

    monkeypatch.setitem(
        sys.modules, "onnxruntime", SimpleNamespace(SessionOptions=_Options, InferenceSession=boom)
    )
    original = SimpleNamespace(_model_path="reid.onnx")
    reid = _reid(session=original, use_io_binding=False)
    assert _calm_onnx_session(reid) is False
    assert reid.model.backend.session is original
