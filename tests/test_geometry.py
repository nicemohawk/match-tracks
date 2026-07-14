"""Unit tests for match_tracks.geometry.

Pure-stdlib synthetic fixtures: fields are generated in a local east/north
plane and projected to lat/lon so the fitter has to recover the geometry it
started from.
"""

import math
import random

import pytest

from match_tracks import geometry
from match_tracks.geometry import (
    EARTH_RADIUS_M,
    FittedRect,
    average_headings_deg,
    convex_hull,
    fit_rect_to_coordinates,
    fit_track_as_field_observation,
    fold_heading_deg,
    haversine_distance_m,
    headings_equivalent,
    minimum_area_rect,
    rect_within_sanity_bounds,
    rects_match_same_field,
)


# --------------------------------------------------------------------------
# Synthetic field helpers
# --------------------------------------------------------------------------

def _local_xy_to_lat_lon(reference_lat, reference_lon, x_meters, y_meters):
    """Inverse of geometry's equirectangular projection around a reference."""
    cos_reference = math.cos(math.radians(reference_lat))
    latitude = reference_lat + math.degrees(y_meters / EARTH_RADIUS_M)
    longitude = reference_lon + math.degrees(
        x_meters / (EARTH_RADIUS_M * cos_reference))
    return latitude, longitude


def _long_axis_unit_vectors(compass_heading_deg):
    """Return (long_axis_unit, short_axis_unit) in local xy for a compass
    heading of the long axis."""
    axis_angle_rad = math.radians(90.0 - compass_heading_deg)
    long_axis = (math.cos(axis_angle_rad), math.sin(axis_angle_rad))
    short_axis = (-long_axis[1], long_axis[0])
    return long_axis, short_axis


def make_field_coordinates(reference_lat, reference_lon, length_m, width_m,
                           heading_deg, interior=False, point_count=0,
                           seed=0):
    """Build a list of [lat, lon] points for a rectangular field.

    When ``interior`` is False the points trace the perimeter (corners plus
    edge samples). When True, ``point_count`` random interior points are
    scattered inside the rectangle.
    """
    long_axis, short_axis = _long_axis_unit_vectors(heading_deg)
    half_length = length_m / 2.0
    half_width = width_m / 2.0

    def local_to_coordinate(along_long, along_short):
        x = along_long * long_axis[0] + along_short * short_axis[0]
        y = along_long * long_axis[1] + along_short * short_axis[1]
        return list(_local_xy_to_lat_lon(reference_lat, reference_lon, x, y))

    coordinates = []
    if interior:
        rng = random.Random(seed)
        # Always pin the four corners so the extent is well defined.
        for along_long in (-half_length, half_length):
            for along_short in (-half_width, half_width):
                coordinates.append(local_to_coordinate(along_long, along_short))
        for _ in range(point_count):
            along_long = rng.uniform(-half_length, half_length)
            along_short = rng.uniform(-half_width, half_width)
            coordinates.append(local_to_coordinate(along_long, along_short))
    else:
        samples = 12
        for step in range(samples):
            fraction = step / float(samples)
            edge = -half_length + fraction * length_m
            coordinates.append(local_to_coordinate(edge, -half_width))
            coordinates.append(local_to_coordinate(edge, half_width))
        for step in range(samples):
            fraction = step / float(samples)
            edge = -half_width + fraction * width_m
            coordinates.append(local_to_coordinate(-half_length, edge))
            coordinates.append(local_to_coordinate(half_length, edge))
    return coordinates


# --------------------------------------------------------------------------
# haversine
# --------------------------------------------------------------------------

def test_haversine_one_degree_latitude():
    distance = haversine_distance_m(39.0, -82.0, 40.0, -82.0)
    assert distance == pytest.approx(111195.0, abs=5.0)


def test_haversine_zero_distance():
    assert haversine_distance_m(10.0, 20.0, 10.0, 20.0) == pytest.approx(0.0)


def test_haversine_symmetric():
    forward = haversine_distance_m(39.33, -82.10, 39.34, -82.11)
    backward = haversine_distance_m(39.34, -82.11, 39.33, -82.10)
    assert forward == pytest.approx(backward)


# --------------------------------------------------------------------------
# folding and equivalence
# --------------------------------------------------------------------------

def test_fold_heading_into_axis_domain():
    assert fold_heading_deg(0.0) == pytest.approx(0.0)
    assert fold_heading_deg(190.0) == pytest.approx(10.0)
    assert fold_heading_deg(180.0) == pytest.approx(0.0)
    assert fold_heading_deg(359.0) == pytest.approx(179.0)


def test_headings_equivalent_across_wrap():
    assert headings_equivalent(179.0, 1.0)


def test_headings_not_equivalent_when_far():
    assert not headings_equivalent(95.0, 80.0)


def test_headings_equivalent_under_180_flip():
    assert headings_equivalent(30.0, 210.0)
    assert headings_equivalent(30.0, 33.0)
    assert not headings_equivalent(30.0, 55.0)


# --------------------------------------------------------------------------
# average_headings_deg
# --------------------------------------------------------------------------

def test_average_headings_wraps_to_zero_not_ninety():
    result = average_headings_deg(179.0, 1.0, 1.0, 1.0)
    folded = min(result, 180.0 - result)
    assert folded == pytest.approx(0.0, abs=0.01)


def test_average_headings_weighted():
    result = average_headings_deg(10.0, 9.0, 20.0, 1.0)
    assert result == pytest.approx(11.0, abs=0.1)


def test_average_headings_nine_to_one_pulls_toward_heavy():
    result = average_headings_deg(20.0, 9.0, 80.0, 1.0)
    # The heavy weight at 20 dominates; result sits near 20, not the midpoint.
    assert abs(result - 20.0) < abs(result - 50.0)


# --------------------------------------------------------------------------
# convex hull
# --------------------------------------------------------------------------

def test_convex_hull_of_square_with_interior_and_duplicates():
    points = [
        (0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0),
        (5.0, 5.0),           # interior point, must be dropped
        (0.0, 0.0), (10.0, 0.0),  # duplicates
    ]
    hull = convex_hull(points)
    assert len(hull) == 4
    assert set(hull) == {(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)}


def test_convex_hull_counter_clockwise_orientation():
    hull = convex_hull([(0.0, 0.0), (4.0, 0.0), (4.0, 2.0), (0.0, 2.0)])
    signed_area = 0.0
    for index in range(len(hull)):
        x1, y1 = hull[index]
        x2, y2 = hull[(index + 1) % len(hull)]
        signed_area += x1 * y2 - x2 * y1
    assert signed_area > 0  # positive => counter-clockwise


# --------------------------------------------------------------------------
# minimum_area_rect
# --------------------------------------------------------------------------

def _rectangle_points(length, width, angle_deg):
    """Corner points of a length x width rectangle rotated by angle_deg."""
    angle_rad = math.radians(angle_deg)
    cos_a, sin_a = math.cos(angle_rad), math.sin(angle_rad)
    half_length, half_width = length / 2.0, width / 2.0
    corners = []
    for along_long, along_short in ((-half_length, -half_width),
                                    (half_length, -half_width),
                                    (half_length, half_width),
                                    (-half_length, half_width)):
        x = along_long * cos_a - along_short * sin_a
        y = along_long * sin_a + along_short * cos_a
        corners.append((x, y))
    return corners


def test_min_area_rect_axis_aligned():
    points = _rectangle_points(20.0, 8.0, 0.0)
    (center, long_side, short_side, axis_angle_rad) = minimum_area_rect(points)
    assert center[0] == pytest.approx(0.0, abs=1e-6)
    assert center[1] == pytest.approx(0.0, abs=1e-6)
    assert long_side == pytest.approx(20.0, abs=1e-6)
    assert short_side == pytest.approx(8.0, abs=1e-6)
    # Long side runs along +x => axis angle 0 (mod 180).
    folded = math.degrees(axis_angle_rad) % 180.0
    assert min(folded, 180.0 - folded) == pytest.approx(0.0, abs=1e-4)


def test_min_area_rect_rotated_thirty_degrees():
    points = _rectangle_points(20.0, 8.0, 30.0)
    (_, long_side, short_side, axis_angle_rad) = minimum_area_rect(points)
    assert long_side == pytest.approx(20.0, abs=1e-4)
    assert short_side == pytest.approx(8.0, abs=1e-4)
    recovered_deg = math.degrees(axis_angle_rad) % 180.0
    assert recovered_deg == pytest.approx(30.0, abs=1e-3)


# --------------------------------------------------------------------------
# fit_rect_to_coordinates
# --------------------------------------------------------------------------

@pytest.mark.parametrize("heading_deg", [0.0, 25.0, 60.0, 95.0, 130.0, 170.0])
def test_fit_rect_recovers_field(heading_deg):
    reference_lat, reference_lon = 39.33, -82.10
    length_m, width_m = 100.0, 64.0
    coordinates = make_field_coordinates(
        reference_lat, reference_lon, length_m, width_m, heading_deg)

    fit = fit_rect_to_coordinates(coordinates)
    assert fit is not None
    assert fit.length_m == pytest.approx(length_m, abs=1.0)
    assert fit.width_m == pytest.approx(width_m, abs=1.0)
    assert headings_equivalent(fit.heading_deg, heading_deg, tolerance_deg=2.0)
    assert haversine_distance_m(
        fit.center_lat, fit.center_lon, reference_lat, reference_lon) < 1.0


def test_fit_rect_too_few_distinct_points():
    assert fit_rect_to_coordinates([[39.0, -82.0], [39.0, -82.0]]) is None


def test_fit_rect_collinear_returns_none():
    collinear = [[39.0 + i * 0.0001, -82.0] for i in range(10)]
    assert fit_rect_to_coordinates(collinear) is None


# --------------------------------------------------------------------------
# fit_track_as_field_observation
# --------------------------------------------------------------------------

def test_track_observation_rejects_short_track():
    coordinates = make_field_coordinates(
        39.33, -82.10, 95.0, 60.0, 40.0, interior=True, point_count=50, seed=1)
    assert len(coordinates) < geometry.MINIMUM_TRACK_POINTS_FOR_OBSERVATION
    assert fit_track_as_field_observation(coordinates) is None


def test_track_observation_accepts_plausible_field():
    coordinates = make_field_coordinates(
        39.33, -82.10, 95.0, 60.0, 40.0, interior=True, point_count=300, seed=2)
    assert len(coordinates) >= geometry.MINIMUM_TRACK_POINTS_FOR_OBSERVATION
    fit = fit_track_as_field_observation(coordinates)
    assert fit is not None
    assert rect_within_sanity_bounds(fit)
    # Expanded fit should exceed the raw interior extent.
    assert fit.length_m >= 95.0
    assert fit.width_m >= 60.0


def test_track_observation_rejects_warmup_box():
    coordinates = make_field_coordinates(
        39.33, -82.10, 20.0, 10.0, 40.0, interior=True, point_count=300, seed=3)
    assert len(coordinates) >= geometry.MINIMUM_TRACK_POINTS_FOR_OBSERVATION
    assert fit_track_as_field_observation(coordinates) is None


# --------------------------------------------------------------------------
# rect_within_sanity_bounds
# --------------------------------------------------------------------------

def test_sanity_bounds_accepts_pitch():
    assert rect_within_sanity_bounds(
        FittedRect(39.0, -82.0, 100.0, 64.0, 20.0))


def test_sanity_bounds_rejects_out_of_range():
    assert not rect_within_sanity_bounds(
        FittedRect(39.0, -82.0, 40.0, 20.0, 20.0))
    assert not rect_within_sanity_bounds(
        FittedRect(39.0, -82.0, 100.0, 100.0, 20.0))


# --------------------------------------------------------------------------
# rects_match_same_field
# --------------------------------------------------------------------------

def test_rects_match_positive():
    a = FittedRect(39.33, -82.10, 100.0, 64.0, 30.0)
    b = FittedRect(39.3301, -82.1001, 105.0, 62.0, 33.0)
    assert rects_match_same_field(a, b)


def test_rects_match_under_180_flip():
    a = FittedRect(39.33, -82.10, 100.0, 64.0, 5.0)
    b = FittedRect(39.33, -82.10, 100.0, 64.0, 185.0)
    assert rects_match_same_field(a, b)


def test_rects_no_match_far_center():
    a = FittedRect(39.33, -82.10, 100.0, 64.0, 30.0)
    b = FittedRect(39.34, -82.10, 100.0, 64.0, 30.0)
    assert not rects_match_same_field(a, b)


def test_rects_no_match_different_dimensions():
    a = FittedRect(39.33, -82.10, 100.0, 64.0, 30.0)
    b = FittedRect(39.33, -82.10, 120.0, 64.0, 30.0)
    assert not rects_match_same_field(a, b)


def test_rects_no_match_different_heading():
    a = FittedRect(39.33, -82.10, 100.0, 64.0, 30.0)
    b = FittedRect(39.33, -82.10, 100.0, 64.0, 55.0)
    assert not rects_match_same_field(a, b)
