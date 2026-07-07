"""3D CNN action classifier (torchvision ``r3d_18``) for concealment/cash actions."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import torch
from torch import nn
from torchvision.models.video import R3D_18_Weights, r3d_18

from storeguard.config import pick_device

KINETICS_MEAN = (0.43216, 0.394666, 0.37645)
KINETICS_STD = (0.22803, 0.22145, 0.216989)


class ActionClassifier:
    """Video action classifier: ``r3d_18`` backbone with a custom class head.

    Wraps a Kinetics-400 pretrained ResNet3D-18, with the final fully connected
    layer replaced to predict this project's action classes (e.g.
    ``["normal", "pocket", "take_cash"]``). The model is in eval mode by
    default; training code may switch ``self.model`` to train mode.
    """

    def __init__(self, classes: list[str], device: str = "auto", pretrained: bool = True) -> None:
        """Build the network.

        Args:
            classes: Ordered class names; the head outputs one logit per class.
            device: ``"auto"`` (pick cuda/mps/cpu), or an explicit device name.
            pretrained: Load Kinetics-400 pretrained backbone weights.
        """
        if not classes:
            raise ValueError("classes must be a non-empty list of class names")
        self.classes: list[str] = list(classes)
        self.device: str = pick_device(device)
        weights = R3D_18_Weights.KINETICS400_V1 if pretrained else None
        self.model: nn.Module = r3d_18(weights=weights)
        self.model.fc = nn.Linear(512, len(self.classes))
        self.model.to(self.device)
        self.model.eval()

    @torch.no_grad()
    def predict(self, clip: np.ndarray) -> dict[str, float]:
        """Classify one clip and return per-class probabilities.

        Args:
            clip: ``np.ndarray`` of shape ``(T, H, W, 3)``, uint8, RGB
                (as produced by :class:`~storeguard.actions.clipbuffer.ClipBuffer`).

        Returns:
            Mapping ``{class_name: probability}`` (softmax over classes).
        """
        x = np.asarray(clip, dtype=np.float32) / 255.0
        mean = np.asarray(KINETICS_MEAN, dtype=np.float32)
        std = np.asarray(KINETICS_STD, dtype=np.float32)
        x = (x - mean) / std  # (T, H, W, 3)
        tensor = torch.from_numpy(x).permute(3, 0, 1, 2).unsqueeze(0)  # (1, 3, T, H, W)
        tensor = tensor.to(self.device)
        logits = self.model(tensor)
        probs = torch.softmax(logits[0], dim=0).cpu().tolist()
        return {name: float(p) for name, p in zip(self.classes, probs)}

    def save(self, path: str) -> None:
        """Save weights + class names to ``path`` (parent dirs are created)."""
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        torch.save(
            {
                "state_dict": self.model.state_dict(),
                "classes": self.classes,
                "arch": "r3d_18",
            },
            out,
        )

    @classmethod
    def load(cls, path: str, device: str = "auto") -> "ActionClassifier":
        """Load a classifier previously written by :meth:`save`.

        Args:
            path: Checkpoint file path.
            device: Device preference, same semantics as ``__init__``.
        """
        ckpt = torch.load(path, map_location="cpu", weights_only=True)
        arch = ckpt.get("arch")
        if arch != "r3d_18":
            raise ValueError(f"unsupported checkpoint arch {arch!r} in {path} (expected 'r3d_18')")
        clf = cls(classes=list(ckpt["classes"]), device=device, pretrained=False)
        clf.model.load_state_dict(ckpt["state_dict"])
        clf.model.to(clf.device)
        clf.model.eval()
        return clf
