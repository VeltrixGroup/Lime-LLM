"""Cross-camera person re-identification: one global id per person.

ByteTrack ids are per camera (and per track — a person lost behind a shelf
comes back with a new one). This module gives each person a *global* id that
is shared by every camera of the store:

* :class:`AppearanceEncoder` turns a person crop into an appearance
  signature: a deep embedding (ImageNet ResNet by default, or any TorchScript
  person-ReID model) plus clothing color histograms of the upper and lower
  body.
* :class:`IdentityRegistry` is shared by all cameras. It keeps a small
  gallery of signatures per global id and, when a camera starts a new local
  track, matches it against people seen recently. A person who was ``id 1``
  on the hall camera is ``id 1`` again on the checkout camera, and again
  after a ByteTrack id switch on the same camera.

torch / torchvision are imported lazily so importing this module stays cheap.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field

import cv2
import numpy as np
from rich.console import Console

from .config import ReidCfg
from .types import Track

_console = Console()

#: Weight of the deep embedding vs. the color histograms in the combined
#: similarity (they sum to 1, so the similarity stays in 0..1).
_W_DEEP = 0.6
_W_COLOR = 0.4

#: Crops smaller than this (pixels) are too blurry to describe anyone.
_MIN_BOX_H = 40
_MIN_BOX_W = 16

#: Deep embeddings of *any* two people are fairly similar in raw ImageNet
#: feature space. Subtracting the running mean of everything seen so far
#: ("centering") is what makes cosine similarity discriminate between
#: people; until that mean has seen enough crops no match is attempted.
_MEAN_WARMUP = 30

#: A new local track is "unconfirmed" for its first few embeddings: during
#: that window it may still be merged into a better-matching existing id
#: (its very first crop is often a partial body entering the frame).
_CONFIRM_AFTER = 5
#: Confirmed tracks refresh their gallery on every Nth update only.
_REFRESH_EVERY = 5
#: A local track missing for this long is unbound (its global id lives on).
_LOST_SEC = 5.0
#: Gallery size per identity, and how different a new signature must be
#: from the stored ones to be worth keeping.
_GALLERY_MAX = 20
_GALLERY_NOVELTY = 0.92


@dataclass
class Signature:
    """Appearance of one person crop.

    ``deep`` is L2-normalized (or ``None`` when no deep model is available);
    ``color`` holds square-rooted histograms, L2-normalized, so the dot
    product of two of them is their Bhattacharyya coefficient.
    """

    deep: np.ndarray | None
    color: np.ndarray


def _color_signature(crop: np.ndarray) -> np.ndarray:
    """Hue/saturation + brightness histograms of the torso and the legs."""
    h, w = crop.shape[:2]
    # Central columns only: the box edges are mostly shelf / floor.
    x0, x1 = int(w * 0.2), max(int(w * 0.8), int(w * 0.2) + 1)
    parts = (
        crop[int(h * 0.15) : max(int(h * 0.55), int(h * 0.15) + 1), x0:x1],
        crop[int(h * 0.55) : max(int(h * 0.95), int(h * 0.55) + 1), x0:x1],
    )
    out = []
    for part in parts:
        hsv = cv2.cvtColor(part, cv2.COLOR_BGR2HSV)
        # Hue is meaningless for grey / black / white clothes — describe those
        # by brightness instead.
        colored = cv2.inRange(hsv, (0, 50, 40), (180, 256, 256))
        hs = cv2.calcHist([hsv], [0, 1], colored, [16, 4], [0, 180, 50, 256]).ravel()
        v = cv2.calcHist([hsv], [2], cv2.bitwise_not(colored), [8], [0, 256]).ravel()
        hist = np.concatenate([hs, v]).astype(np.float32)
        total = float(hist.sum())
        if total > 0:
            hist /= total
        hist = np.sqrt(hist)
        norm = float(np.linalg.norm(hist))
        if norm > 0:
            hist /= norm
        out.append(hist)
    return (np.concatenate(out) / np.sqrt(2.0)).astype(np.float32)


class AppearanceEncoder:
    """Person crop -> :class:`Signature`. One instance is shared by all cameras."""

    _INPUT_HW = (256, 128)

    def __init__(self, cfg: ReidCfg, device: str) -> None:
        self.cfg = cfg
        self.device = device
        self._lock = threading.Lock()
        self._model = None
        self._torch = None
        try:
            self._model = self._load_model()
        except Exception as exc:  # noqa: BLE001 — degrade to color-only ReID
            _console.print(
                f"[yellow]ReID: could not load the appearance model ({exc}); "
                "matching people across cameras by clothing colors only.[/yellow]"
            )

    def _load_model(self):
        import torch

        self._torch = torch
        name = self.cfg.model
        if name == "auto":
            name = "resnet50" if self.device.startswith("cuda") else "resnet18"
        if name in ("resnet18", "resnet50"):
            import torchvision

            weights = {
                "resnet18": torchvision.models.ResNet18_Weights.DEFAULT,
                "resnet50": torchvision.models.ResNet50_Weights.DEFAULT,
            }[name]
            model = getattr(torchvision.models, name)(weights=weights)
            model.fc = torch.nn.Identity()
        else:
            model = torch.jit.load(name, map_location=self.device)
        model = model.to(self.device).eval()
        _console.print(f"[green]ReID model: {name} on {self.device}[/green]")
        return model

    def encode(self, frame: np.ndarray, boxes: list[tuple[float, float, float, float]]) -> list[Signature | None]:
        """Signatures for ``boxes`` in ``frame`` (``None`` for unusable crops)."""
        fh, fw = frame.shape[:2]
        crops: list[np.ndarray | None] = []
        for x1, y1, x2, y2 in boxes:
            xa, ya = max(0, int(x1)), max(0, int(y1))
            xb, yb = min(fw, int(x2)), min(fh, int(y2))
            if yb - ya < _MIN_BOX_H or xb - xa < _MIN_BOX_W:
                crops.append(None)
            else:
                crops.append(frame[ya:yb, xa:xb])

        valid = [i for i, c in enumerate(crops) if c is not None]
        out: list[Signature | None] = [None] * len(boxes)
        if not valid:
            return out
        deep = self._deep([crops[i] for i in valid]) if self._model is not None else None
        for k, i in enumerate(valid):
            out[i] = Signature(
                deep=None if deep is None else deep[k],
                color=_color_signature(crops[i]),
            )
        return out

    def _deep(self, crops: list[np.ndarray]) -> np.ndarray | None:
        torch = self._torch
        h, w = self._INPUT_HW
        batch = np.stack(
            [cv2.cvtColor(cv2.resize(c, (w, h), interpolation=cv2.INTER_LINEAR), cv2.COLOR_BGR2RGB) for c in crops]
        ).astype(np.float32) / 255.0
        batch = (batch - np.array([0.485, 0.456, 0.406], np.float32)) / np.array(
            [0.229, 0.224, 0.225], np.float32
        )
        try:
            with self._lock, torch.inference_mode():
                x = torch.from_numpy(batch.transpose(0, 3, 1, 2).copy()).to(self.device)
                feats = self._model(x).float().cpu().numpy()
        except Exception as exc:  # noqa: BLE001 — never kill the camera loop
            _console.print(f"[yellow]ReID: embedding failed ({exc})[/yellow]")
            return None
        feats = feats.reshape(len(crops), -1)
        norms = np.linalg.norm(feats, axis=1, keepdims=True)
        return feats / np.maximum(norms, 1e-6)


_encoders: dict[tuple[str, str], AppearanceEncoder] = {}
_encoders_lock = threading.Lock()


def get_encoder(cfg: ReidCfg, device: str) -> AppearanceEncoder:
    """Process-wide encoder per (model, device), so N cameras share one model."""
    key = (cfg.model, device)
    with _encoders_lock:
        enc = _encoders.get(key)
        if enc is None:
            enc = AppearanceEncoder(cfg, device)
            _encoders[key] = enc
        return enc


@dataclass
class _Identity:
    gid: int
    last_seen: float
    gallery: list[Signature] = field(default_factory=list)


@dataclass
class _Binding:
    """One camera's local track currently shown as global id ``gid``."""

    gid: int
    last_seen: float
    updates: int = 0
    embeddings: int = 0
    confirmed: bool = False
    #: The identity was created for this track (not matched to anyone), so
    #: it may still be folded into an existing id while unconfirmed.
    private: bool = True


class IdentityRegistry:
    """Global person ids shared by every camera of one dashboard / runner.

    Thread-safe: every camera thread calls :meth:`assign` on the same
    instance. Camera keys are arbitrary strings (session ids, camera names).
    """

    def __init__(self, cfg: ReidCfg | None = None) -> None:
        self.cfg = cfg or ReidCfg()
        #: Distinguishes this registry's ids from earlier runs: ids restart at
        #: 1 on every reconnect, so "person 1" alone would merge different
        #: people in the cabinet's event history.
        self.run_id = uuid.uuid4().hex[:8]
        self._lock = threading.Lock()
        self._next_gid = 1
        self._identities: dict[int, _Identity] = {}
        self._bindings: dict[str, dict[int, _Binding]] = {}
        self._mean: np.ndarray | None = None
        self._mean_n = 0

    # -- queries ---------------------------------------------------------

    def wants_signature(self, camera: str, local_id: int) -> bool:
        """Whether :meth:`assign` would use a fresh signature for this track."""
        with self._lock:
            b = self._bindings.get(camera, {}).get(local_id)
        if b is None or not b.confirmed:
            return True
        return b.updates % _REFRESH_EVERY == 0

    def person_id(self, gid: int) -> str:
        """Store-wide person id for events, unique across runs (e.g. ``3f9a1c2e-7``)."""
        return f"{self.run_id}-{gid}"

    # -- lifecycle -------------------------------------------------------

    def forget_camera(self, camera: str) -> None:
        """Unbind every local track of ``camera`` (its tracker was reset)."""
        with self._lock:
            self._bindings.pop(camera, None)

    # -- core ------------------------------------------------------------

    def assign(
        self,
        camera: str,
        tracks: list[Track],
        signatures: dict[int, Signature],
        ts: float,
    ) -> dict[int, int]:
        """Map this frame's local track ids to global ids.

        Args:
            camera: Key of the camera the tracks come from.
            tracks: All tracks of the current frame (local ByteTrack ids).
            signatures: Appearance of some of them, by local id (see
                :meth:`wants_signature`); tracks without one keep whatever
                id they already have, or get a new one.
            ts: Frame time (unix seconds).
        """
        with self._lock:
            for sig in signatures.values():
                if sig.deep is not None:
                    self._update_mean(sig.deep)

            self._expire(ts)
            bindings = self._bindings.setdefault(camera, {})
            present = {t.track_id for t in tracks}
            for lid in [lid for lid, b in bindings.items() if lid not in present]:
                if ts - bindings[lid].last_seen > _LOST_SEC:
                    del bindings[lid]

            result: dict[int, int] = {}
            new_ids: list[int] = []
            for t in tracks:
                b = bindings.get(t.track_id)
                if b is None:
                    new_ids.append(t.track_id)
                    continue
                b.last_seen = ts
                b.updates += 1
                sig = signatures.get(t.track_id)
                if sig is not None:
                    b.embeddings += 1
                    if not b.confirmed and b.private:
                        self._try_merge(camera, bindings, b, sig)
                    self._add_to_gallery(self._identities[b.gid], sig)
                    if b.embeddings >= _CONFIRM_AFTER:
                        b.confirmed = True
                self._identities[b.gid].last_seen = ts
                result[t.track_id] = b.gid

            self._assign_new(camera, bindings, new_ids, signatures, ts, result)
            return result

    def _assign_new(
        self,
        camera: str,
        bindings: dict[int, _Binding],
        new_ids: list[int],
        signatures: dict[int, Signature],
        ts: float,
        result: dict[int, int],
    ) -> None:
        taken = {b.gid for b in bindings.values()}
        pairs: list[tuple[float, int, int]] = []
        if self._matching_ready():
            for lid in new_ids:
                sig = signatures.get(lid)
                if sig is None:
                    continue
                for gid, ident in self._identities.items():
                    if gid in taken:
                        continue
                    s = self._similarity(sig, ident)
                    if s >= self.cfg.threshold:
                        pairs.append((s, lid, gid))
        pairs.sort(reverse=True)
        matched: dict[int, int] = {}
        used: set[int] = set()
        for _s, lid, gid in pairs:
            if lid in matched or gid in used:
                continue
            matched[lid] = gid
            used.add(gid)

        for lid in new_ids:
            sig = signatures.get(lid)
            if lid in matched:
                gid = matched[lid]
                private = False
            else:
                gid = self._next_gid
                self._next_gid += 1
                self._identities[gid] = _Identity(gid=gid, last_seen=ts)
                private = True
            b = _Binding(gid=gid, last_seen=ts, updates=1, private=private)
            if sig is not None:
                b.embeddings = 1
                self._add_to_gallery(self._identities[gid], sig)
            self._identities[gid].last_seen = ts
            bindings[lid] = b
            result[lid] = gid

    def _try_merge(
        self, camera: str, bindings: dict[int, _Binding], b: _Binding, sig: Signature
    ) -> None:
        """Fold a young, self-created id into an existing one it matches."""
        if not self._matching_ready():
            return
        own = b.gid
        shown = sum(1 for cam in self._bindings.values() for x in cam.values() if x.gid == own)
        if shown > 1:
            return  # another track already shows this id — not private anymore
        taken = {x.gid for x in bindings.values()}
        best_gid, best_s = None, self.cfg.threshold
        for gid, ident in self._identities.items():
            if gid == own or gid in taken:
                continue
            s = self._similarity(sig, ident)
            if s >= best_s:
                best_gid, best_s = gid, s
        if best_gid is None:
            return
        old = self._identities.pop(own)
        target = self._identities[best_gid]
        for g in old.gallery:
            self._add_to_gallery(target, g)
        b.gid = best_gid
        b.private = False

    # -- helpers ---------------------------------------------------------

    def _expire(self, ts: float) -> None:
        bound = {b.gid for cam in self._bindings.values() for b in cam.values()}
        for gid in [
            gid
            for gid, ident in self._identities.items()
            if gid not in bound and ts - ident.last_seen > self.cfg.ttl_sec
        ]:
            del self._identities[gid]

    def _update_mean(self, deep: np.ndarray) -> None:
        self._mean_n += 1
        if self._mean is None or self._mean.shape != deep.shape:
            self._mean = deep.astype(np.float32).copy()
            self._mean_n = 1
            return
        alpha = max(1.0 / self._mean_n, 0.01)
        self._mean = (1.0 - alpha) * self._mean + alpha * deep

    def _matching_ready(self) -> bool:
        # Color-only mode needs no warm-up; deep matching needs a mean.
        return self._mean is None or self._mean_n >= _MEAN_WARMUP

    def _centered(self, v: np.ndarray) -> np.ndarray:
        if self._mean is None or self._mean.shape != v.shape:
            return v
        c = v - self._mean
        return c / max(float(np.linalg.norm(c)), 1e-6)

    def _similarity(self, sig: Signature, ident: _Identity) -> float:
        if not ident.gallery:
            return 0.0
        color_s = float(np.max(np.stack([g.color for g in ident.gallery]) @ sig.color))
        deeps = [g.deep for g in ident.gallery if g.deep is not None]
        if sig.deep is None or not deeps:
            return color_s
        q = self._centered(sig.deep)
        deep_s = float(np.max(np.stack([self._centered(d) for d in deeps]) @ q))
        # Centered cosine is in -1..1; clamp so the blend stays in 0..1.
        return _W_DEEP * max(deep_s, 0.0) + _W_COLOR * color_s

    def _add_to_gallery(self, ident: _Identity, sig: Signature) -> None:
        for g in ident.gallery:
            same_color = float(g.color @ sig.color) > _GALLERY_NOVELTY
            same_deep = (
                g.deep is None
                or sig.deep is None
                or float(g.deep @ sig.deep) > _GALLERY_NOVELTY
            )
            if same_color and same_deep:
                return  # nothing new about this view
        ident.gallery.append(sig)
        if len(ident.gallery) > _GALLERY_MAX:
            ident.gallery.pop(0)


def assign_global_ids(
    registry: IdentityRegistry,
    encoder: AppearanceEncoder | None,
    camera: str,
    frame: np.ndarray,
    tracks: list[Track],
    ts: float,
) -> list[Track]:
    """Replace local ByteTrack ids in ``tracks`` with global person ids."""
    if not tracks:
        registry.assign(camera, tracks, {}, ts)
        return tracks
    need = [t for t in tracks if registry.wants_signature(camera, t.track_id)]
    signatures: dict[int, Signature] = {}
    if need and encoder is not None:
        for t, sig in zip(need, encoder.encode(frame, [t.box for t in need])):
            if sig is not None:
                signatures[t.track_id] = sig
    mapping = registry.assign(camera, tracks, signatures, ts)
    return [
        Track(track_id=mapping.get(t.track_id, t.track_id), box=t.box, conf=t.conf)
        for t in tracks
    ]
