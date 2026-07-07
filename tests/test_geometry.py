"""Tests for :mod:`storeguard.geometry`.

Covers ray-casting point-in-polygon (convex, concave and near-edge cases),
``Zone.contains`` with normalized-to-pixel scaling, ``Zone.pixel_points``
and ``zones_from_cfg``.
"""

from __future__ import annotations

from storeguard.geometry import Zone, point_in_polygon, zones_from_cfg

UNIT_SQUARE: list[tuple[float, float]] = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
TRIANGLE: list[tuple[float, float]] = [(0.0, 0.0), (4.0, 0.0), (0.0, 4.0)]
# Concave "L": full width for y in [0, 2], left half for y in [2, 4].
L_SHAPE: list[tuple[float, float]] = [
    (0.0, 0.0),
    (4.0, 0.0),
    (4.0, 2.0),
    (2.0, 2.0),
    (2.0, 4.0),
    (0.0, 4.0),
]


class TestPointInPolygon:
    """point_in_polygon on squares, triangles and a concave polygon."""

    def test_inside_square(self) -> None:
        assert point_in_polygon((0.5, 0.5), UNIT_SQUARE)
        assert point_in_polygon((0.1, 0.9), UNIT_SQUARE)

    def test_outside_square(self) -> None:
        assert not point_in_polygon((1.5, 0.5), UNIT_SQUARE)
        assert not point_in_polygon((-0.5, 0.5), UNIT_SQUARE)
        assert not point_in_polygon((0.5, 2.0), UNIT_SQUARE)
        assert not point_in_polygon((0.5, -1.0), UNIT_SQUARE)

    def test_near_edge_inside(self) -> None:
        assert point_in_polygon((0.999, 0.5), UNIT_SQUARE)
        assert point_in_polygon((0.5, 0.001), UNIT_SQUARE)
        assert point_in_polygon((0.001, 0.001), UNIT_SQUARE)

    def test_near_edge_outside(self) -> None:
        assert not point_in_polygon((1.001, 0.5), UNIT_SQUARE)
        assert not point_in_polygon((0.5, -0.001), UNIT_SQUARE)
        assert not point_in_polygon((-0.001, 1.001), UNIT_SQUARE)

    def test_triangle(self) -> None:
        assert point_in_polygon((1.0, 1.0), TRIANGLE)
        # Just below the hypotenuse x + y = 4.
        assert point_in_polygon((1.9, 1.9), TRIANGLE)
        # Just above the hypotenuse.
        assert not point_in_polygon((2.1, 2.1), TRIANGLE)
        assert not point_in_polygon((5.0, 5.0), TRIANGLE)

    def test_concave_polygon(self) -> None:
        # Both arms of the "L" are inside.
        assert point_in_polygon((3.0, 1.0), L_SHAPE)
        assert point_in_polygon((1.0, 3.0), L_SHAPE)
        # The notch (top-right quadrant) is outside.
        assert not point_in_polygon((3.0, 3.0), L_SHAPE)


class TestZone:
    """Zone: normalized vertices scaled to pixel coordinates per frame size."""

    def test_name_is_kept(self) -> None:
        zone = Zone("shelf-1", UNIT_SQUARE)
        assert zone.name == "shelf-1"

    def test_contains_scales_to_frame(self) -> None:
        # Left half of the frame.
        zone = Zone("left", [(0.0, 0.0), (0.5, 0.0), (0.5, 1.0), (0.0, 1.0)])
        assert zone.contains((100.0, 240.0), 640, 480)
        assert not zone.contains((400.0, 240.0), 640, 480)

    def test_contains_same_point_different_frame_sizes(self) -> None:
        # Central square: x, y in [0.25, 0.75] normalized.
        zone = Zone("mid", [(0.25, 0.25), (0.75, 0.25), (0.75, 0.75), (0.25, 0.75)])
        pt = (170.0, 200.0)
        # 640x480: (0.266, 0.417) normalized -> inside.
        assert zone.contains(pt, 640, 480)
        # 200x100: (0.85, 2.0) normalized -> outside.
        assert not zone.contains(pt, 200, 100)

    def test_pixel_points(self) -> None:
        zone = Zone("full", UNIT_SQUARE)
        assert list(zone.pixel_points(200, 100)) == [
            (0, 0),
            (200, 0),
            (200, 100),
            (0, 100),
        ]

    def test_pixel_points_fractional_vertices(self) -> None:
        zone = Zone("tri", [(0.5, 0.5), (1.0, 0.5), (1.0, 1.0)])
        assert list(zone.pixel_points(200, 100)) == [(100, 50), (200, 50), (200, 100)]


def test_zones_from_cfg() -> None:
    """zones_from_cfg turns ZoneCfg models into working Zone objects, in order."""
    from storeguard.config import ZoneCfg

    cfgs = [
        ZoneCfg(name="shelf-1", points=[(0.0, 0.0), (0.5, 0.0), (0.5, 0.5), (0.0, 0.5)]),
        ZoneCfg(name="exit", points=[(0.5, 0.5), (1.0, 0.5), (1.0, 1.0), (0.5, 1.0)]),
    ]
    zones = zones_from_cfg(cfgs)
    assert [z.name for z in zones] == ["shelf-1", "exit"]
    assert all(isinstance(z, Zone) for z in zones)
    assert zones[0].contains((10.0, 10.0), 100, 100)
    assert not zones[0].contains((90.0, 90.0), 100, 100)
    assert zones[1].contains((90.0, 90.0), 100, 100)
