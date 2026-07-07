"""Stage-2 action recognition: clip buffering, 3D CNN model, dataset and training.

Heavy dependencies (torch, torchvision) are only imported when the symbols
that need them are actually accessed, so ``storeguard.actions.clipbuffer``
stays importable with just cv2 + numpy.
"""

from __future__ import annotations

from typing import Any

from storeguard.actions.clipbuffer import ClipBuffer

__all__ = [
    "ActionClassifier",
    "ClipBuffer",
    "ClipDataset",
    "KINETICS_MEAN",
    "KINETICS_STD",
    "make_dataset",
    "train_action",
]

_LAZY_EXPORTS = {
    "ActionClassifier": ("storeguard.actions.model", "ActionClassifier"),
    "KINETICS_MEAN": ("storeguard.actions.model", "KINETICS_MEAN"),
    "KINETICS_STD": ("storeguard.actions.model", "KINETICS_STD"),
    "ClipDataset": ("storeguard.actions.dataset", "ClipDataset"),
    "make_dataset": ("storeguard.actions.dataset", "make_dataset"),
    "train_action": ("storeguard.actions.train", "train_action"),
}


def __getattr__(name: str) -> Any:
    """Lazily resolve torch-backed exports on first access (PEP 562)."""
    try:
        module_name, attr = _LAZY_EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    import importlib

    return getattr(importlib.import_module(module_name), attr)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
