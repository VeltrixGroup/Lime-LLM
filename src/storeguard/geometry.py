"""Polygon zone geometry: point-in-polygon tests and zone helpers.

Pure stdlib + numpy — no OpenCV or torch imports here, so this module is
safe to use from tests and from every other part of the pipeline.
"""

from __future__ import annotations


def point_in_polygon(
    pt: tuple[float, float], polygon: list[tuple[float, float]]
) -> bool:
    """Return True if ``pt`` lies inside ``polygon`` (ray casting).

    Casts a horizontal ray from ``pt`` to +x and counts edge crossings; an
    odd count means the point is inside.  Points exactly on an edge may fall
    on either side — zone polygons should be drawn with a small margin.
    """
    if len(polygon) < 3:
        return False
    x, y = pt
    inside = False
    j = len(polygon) - 1
    for i in range(len(polygon)):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if (yi > y) != (yj > y):
            x_cross = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < x_cross:
                inside = not inside
        j = i
    return inside


class Zone:
    """A named polygon zone with vertices normalized to the 0..1 range."""

    def __init__(self, name: str, points_norm: list[tuple[float, float]]) -> None:
        """Create a zone from normalized (0..1) polygon vertices."""
        self.name = name
        self.points_norm = [(float(x), float(y)) for x, y in points_norm]

    def contains(
        self, pt_px: tuple[float, float], frame_w: int, frame_h: int
    ) -> bool:
        """Test whether a pixel-space point lies inside this zone.

        The normalized vertices are scaled to pixels for the given frame
        size, then a ray-casting point-in-polygon test is applied.
        """
        polygon = [(x * frame_w, y * frame_h) for x, y in self.points_norm]
        return point_in_polygon(pt_px, polygon)

    def pixel_points(self, frame_w: int, frame_h: int) -> list[tuple[int, int]]:
        """Return the polygon vertices in integer pixel coords (for drawing)."""
        return [
            (int(round(x * frame_w)), int(round(y * frame_h)))
            for x, y in self.points_norm
        ]

    def __repr__(self) -> str:
        return f"Zone(name={self.name!r}, vertices={len(self.points_norm)})"


def zones_from_cfg(zone_cfgs: list) -> list[Zone]:
    """Build :class:`Zone` objects from config entries (``list[ZoneCfg]``).

    Duck-typed: each entry only needs ``.name`` and ``.points`` attributes,
    so this module stays free of pydantic/config imports.
    """
    return [Zone(z.name, list(z.points)) for z in zone_cfgs]
