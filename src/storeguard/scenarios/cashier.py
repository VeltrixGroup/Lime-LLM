"""Cashier scenario: detect an employee taking cash out of the register.

Identical machinery to :class:`~storeguard.scenarios.pocket.PocketScenario`
but restricted to ``register*`` zones and triggered by the ``take_cash``
action class.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from storeguard.scenarios.pocket import PocketScenario

if TYPE_CHECKING:
    from storeguard.actions.clipbuffer import ClipBuffer
    from storeguard.actions.model import ActionClassifier
    from storeguard.geometry import Zone


class CashierScenario(PocketScenario):
    """Detect the ``take_cash`` action at the cash register.

    Behaves exactly like :class:`PocketScenario` — per-track clip buffering,
    action classification, 30 s per-track cooldown — except that zone
    filtering uses zones whose name starts with ``"register"`` and the event
    fires when the ``take_cash`` probability reaches the threshold.
    """

    kind: ClassVar[str] = "take_cash"
    zone_prefix: ClassVar[str] = "register"
    target_class: ClassVar[str] = "take_cash"
    message_template: ClassVar[str] = (
        "[{camera}] Suspected cash theft: person #{track_id} may have taken "
        "cash from the register (confidence {score:.2f})."
    )

    def __init__(
        self,
        camera: str,
        model: "ActionClassifier",
        buf: "ClipBuffer",
        threshold: float = 0.80,
        zones: list["Zone"] | None = None,
    ) -> None:
        """Create the scenario for one camera.

        Args:
            camera: Camera name from the config (used in event messages).
            model: Trained action classifier; must expose ``predict(clip)``.
            buf: Per-track rolling clip buffer shared with other scenarios
                on the same camera.
            threshold: Minimum probability of ``take_cash`` to alert.
            zones: Optional camera zones; only zones whose name starts with
                ``"register"`` are used for filtering.
        """
        super().__init__(camera, model, buf, threshold=threshold, zones=zones)
