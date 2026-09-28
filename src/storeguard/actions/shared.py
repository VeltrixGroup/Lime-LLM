"""One trained action classifier per process, shared by every camera.

Used by both the headless runner and the live dashboard: the 3D CNN is
loaded once (lazily, on first use) and every camera thread calls it through
one lock. torch is only imported when weights actually exist.
"""

from __future__ import annotations

import threading
from pathlib import Path

from rich.console import Console

_console = Console()


class SharedActionModel:
    """Thread-safe facade over a single ActionClassifier shared by all cameras.

    The YOLO tracker is per-stream, but the action classifier is one network
    used by every camera thread — all ``predict`` calls are serialized
    through one lock so the model stays thread-safe.
    """

    def __init__(self, model) -> None:
        self._model = model
        self._lock = threading.Lock()

    def predict(self, clip) -> dict[str, float]:
        """Run ``ActionClassifier.predict`` under the shared lock."""
        with self._lock:
            return self._model.predict(clip)


_model_lock = threading.Lock()
_model_cache: dict[str, SharedActionModel | None] = {}


def get_action_model(weights: str) -> SharedActionModel | None:
    """Load the shared action classifier once; ``None`` if weights are missing.

    The result (including the "weights missing" / "failed to load" outcome) is
    cached per weights path, so the model is loaded — and the warning
    printed — at most once no matter how many cameras ask for it.
    """
    with _model_lock:
        if weights in _model_cache:
            return _model_cache[weights]
        if not Path(weights).is_file():
            _console.print(
                f"[yellow]Action model weights not found at '{weights}' — the "
                "'pocket' (hide in pocket/bag) and 'take_cash' (cash from the "
                "register) detections are disabled. Train a model first: "
                "[bold]storeguard train[/bold][/yellow]"
            )
            _model_cache[weights] = None
            return None

        try:
            from .model import ActionClassifier  # heavy import (torch)

            _console.print(f"[cyan]Loading action model from '{weights}'…[/cyan]")
            shared = SharedActionModel(ActionClassifier.load(weights))
        except Exception as exc:  # noqa: BLE001 — a bad file must not kill the cameras
            _console.print(
                f"[red]Could not load the action model '{weights}': {exc} — "
                "'pocket' / 'take_cash' detections are disabled.[/red]"
            )
            shared = None
        _model_cache[weights] = shared
        return shared
