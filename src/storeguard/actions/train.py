"""Fine-tune the action classifier on the store's own labeled clips."""

from __future__ import annotations

import random
from collections import defaultdict
from pathlib import Path

import numpy as np
import torch
from rich.console import Console
from rich.live import Live
from rich.table import Table
from torch import nn
from torch.utils.data import DataLoader, Subset

from storeguard.actions.dataset import ClipDataset
from storeguard.actions.model import ActionClassifier


def train_action(
    data_dir: str,
    out_path: str,
    classes: list[str] | None = None,
    epochs: int = 30,
    batch_size: int = 8,
    lr: float = 1e-4,
    val_split: float = 0.2,
    device: str = "auto",
    seed: int = 42,
) -> dict:
    """Fine-tune ``r3d_18`` on clips under ``data_dir`` and save the best model.

    Uses a stratified train/val split, CrossEntropyLoss with inverse-frequency
    class weights, AdamW and cosine LR annealing. After every epoch a metrics
    row is printed; the checkpoint with the best validation balanced accuracy
    (mean per-class recall) is written to ``out_path`` via
    :meth:`ActionClassifier.save`.

    Args:
        data_dir: Dataset root with one subdirectory of ``.mp4`` clips per class.
        out_path: Destination checkpoint path (parent dirs are created).
        classes: Class names; inferred from ``data_dir`` subdirectories (sorted)
            when None.
        epochs: Number of training epochs.
        batch_size: Mini-batch size.
        lr: AdamW learning rate.
        val_split: Fraction of each class held out for validation.
        device: ``"auto"`` | ``"cpu"`` | ``"cuda"`` | ``"mps"``.
        seed: Seed for the split, shuffling and augmentation RNGs.

    Returns:
        Metrics dict for the best epoch, including the confusion matrix as
        nested lists (rows = true class, columns = predicted class).
    """
    console = Console()
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)

    root = Path(data_dir)
    if not root.is_dir():
        raise ValueError(f"data_dir does not exist or is not a directory: {root}")
    if classes is None:
        classes = sorted(d.name for d in root.iterdir() if d.is_dir())
    if len(classes) < 2:
        raise ValueError(f"need at least 2 classes to train, found {classes!r} in {root}")

    train_ds = ClipDataset(str(root), classes, train=True)
    val_ds = ClipDataset(str(root), classes, train=False)
    # Both datasets index the same sorted file list, so sample indices align.

    train_idx, val_idx = _stratified_split(train_ds.samples, len(classes), val_split, seed)
    if not val_idx:
        raise ValueError("validation split is empty; add more clips or lower val_split")

    train_counts = [0] * len(classes)
    for i in train_idx:
        train_counts[train_ds.samples[i][1]] += 1
    for name, count in zip(classes, train_counts):
        if count == 0:
            raise ValueError(f"class {name!r} has no training samples after the split")

    clf = ActionClassifier(classes, device=device, pretrained=True)
    dev = clf.device
    model = clf.model

    total = sum(train_counts)
    weights = torch.tensor(
        [total / (len(classes) * c) for c in train_counts], dtype=torch.float32, device=dev
    )
    criterion = nn.CrossEntropyLoss(weight=weights)
    optimizer = torch.optim.AdamW(model.parameters(), lr=lr)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs)

    train_loader = DataLoader(
        Subset(train_ds, train_idx), batch_size=batch_size, shuffle=True, num_workers=0
    )
    val_loader = DataLoader(
        Subset(val_ds, val_idx), batch_size=batch_size, shuffle=False, num_workers=0
    )

    console.print(
        f"training on [bold]{len(train_idx)}[/] clips, validating on "
        f"[bold]{len(val_idx)}[/] (classes: {', '.join(classes)}; device: {dev})"
    )

    table = Table(title="Training")
    table.add_column("epoch", justify="right")
    table.add_column("train loss", justify="right")
    table.add_column("val loss", justify="right")
    table.add_column("val acc", justify="right")
    for name in classes:
        table.add_column(f"recall {name}", justify="right")

    best_bal = -1.0
    best: dict = {}

    with Live(table, console=console, refresh_per_second=4):
        for epoch in range(1, epochs + 1):
            train_loss = _run_train_epoch(model, train_loader, criterion, optimizer, dev)
            val_loss, confusion = _run_val_epoch(model, val_loader, criterion, dev, len(classes))
            scheduler.step()

            total_val = int(confusion.sum())
            val_acc = float(np.trace(confusion)) / total_val if total_val else 0.0
            recalls = _per_class_recall(confusion)
            supported = [r for r, row in zip(recalls, confusion) if row.sum() > 0]
            balanced = float(np.mean(supported)) if supported else 0.0

            table.add_row(
                str(epoch),
                f"{train_loss:.4f}",
                f"{val_loss:.4f}",
                f"{val_acc:.3f}",
                *[f"{r:.3f}" for r in recalls],
            )

            if balanced > best_bal:
                best_bal = balanced
                clf.save(out_path)
                best = {
                    "classes": list(classes),
                    "best_epoch": epoch,
                    "epochs": epochs,
                    "train_samples": len(train_idx),
                    "val_samples": len(val_idx),
                    "train_loss": float(train_loss),
                    "val_loss": float(val_loss),
                    "val_acc": val_acc,
                    "val_balanced_acc": balanced,
                    "per_class_recall": {n: float(r) for n, r in zip(classes, recalls)},
                    "confusion_matrix": [[int(v) for v in row] for row in confusion],
                    "checkpoint": str(out_path),
                }

    model.eval()
    console.print(
        f"best epoch [bold]{best['best_epoch']}[/] "
        f"(balanced acc {best['val_balanced_acc']:.3f}) saved to [bold]{out_path}[/]"
    )
    return best


def _stratified_split(
    samples: list[tuple[Path, int]], n_classes: int, val_split: float, seed: int
) -> tuple[list[int], list[int]]:
    """Split sample indices per class; each class with >= 2 clips gets >= 1 val clip."""
    by_class: dict[int, list[int]] = defaultdict(list)
    for i, (_, label) in enumerate(samples):
        by_class[label].append(i)
    rng = random.Random(seed)
    train_idx: list[int] = []
    val_idx: list[int] = []
    for label in sorted(by_class):
        indices = list(by_class[label])
        rng.shuffle(indices)
        if len(indices) >= 2:
            n_val = int(round(len(indices) * val_split))
            n_val = max(1, min(n_val, len(indices) - 1))
        else:
            n_val = 0
        val_idx.extend(indices[:n_val])
        train_idx.extend(indices[n_val:])
    return train_idx, val_idx


def _run_train_epoch(
    model: nn.Module,
    loader: DataLoader,
    criterion: nn.Module,
    optimizer: torch.optim.Optimizer,
    device: str,
) -> float:
    """One optimization pass over the training loader; returns mean sample loss."""
    model.train()
    loss_sum = 0.0
    n_samples = 0
    for clips, labels in loader:
        clips = clips.to(device)
        labels = labels.to(device)
        optimizer.zero_grad(set_to_none=True)
        logits = model(clips)
        loss = criterion(logits, labels)
        loss.backward()
        optimizer.step()
        loss_sum += float(loss.item()) * labels.size(0)
        n_samples += int(labels.size(0))
    return loss_sum / n_samples if n_samples else 0.0


@torch.no_grad()
def _run_val_epoch(
    model: nn.Module,
    loader: DataLoader,
    criterion: nn.Module,
    device: str,
    n_classes: int,
) -> tuple[float, np.ndarray]:
    """Evaluate; returns (mean sample loss, confusion matrix [true x pred])."""
    model.eval()
    loss_sum = 0.0
    n_samples = 0
    confusion = np.zeros((n_classes, n_classes), dtype=np.int64)
    for clips, labels in loader:
        clips = clips.to(device)
        labels = labels.to(device)
        logits = model(clips)
        loss = criterion(logits, labels)
        loss_sum += float(loss.item()) * labels.size(0)
        n_samples += int(labels.size(0))
        preds = logits.argmax(dim=1)
        for t, p in zip(labels.cpu().tolist(), preds.cpu().tolist()):
            confusion[t, p] += 1
    return (loss_sum / n_samples if n_samples else 0.0), confusion


def _per_class_recall(confusion: np.ndarray) -> list[float]:
    """Diagonal / row sums of the confusion matrix (0.0 for empty classes)."""
    recalls: list[float] = []
    for i, row in enumerate(confusion):
        support = int(row.sum())
        recalls.append(float(confusion[i, i]) / support if support else 0.0)
    return recalls
