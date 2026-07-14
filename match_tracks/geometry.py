"""Pure-Python geographic geometry for the community field database.

No third-party dependencies (no numpy/shapely) — everything here operates on
plain floats and lists so it runs anywhere the app runs.

Conventions:
- Coordinates arrive as ``[lat, lon]`` pairs (the wire format used by clients).
- Headings are compass bearings in degrees, folded into ``[0, 180)`` — a pitch
  whose long axis points north-south has the same heading whether you walk it
  south-to-north or north-to-south. ``179.9`` and ``0.1`` are nearly equal.
- Distances and lengths are meters.
"""

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

# Mean Earth radius used by the haversine formula (meters).
EARTH_RADIUS_M = 6371000.8


# Sanity bounds for a plausible soccer pitch (meters).
FIELD_LENGTH_BOUNDS_M = (60.0, 130.0)
FIELD_WIDTH_BOUNDS_M = (30.0, 90.0)

# Proximity thresholds for "same physical field" (spec-defined).
SAME_FIELD_CENTER_DISTANCE_M = 40.0
SAME_FIELD_LENGTH_TOLERANCE_M = 15.0
SAME_FIELD_WIDTH_TOLERANCE_M = 15.0
SAME_FIELD_HEADING_TOLERANCE_DEG = 10.0

# Player tracks under-cover the pitch; expand fitted rectangles by this factor
# per axis before merging/creating field observations.
TRACK_FIT_EXPANSION = 1.05

# Tracks with fewer points than this are ignored as field observations.
MINIMUM_TRACK_POINTS_FOR_OBSERVATION = 200


@dataclass
class FittedRect:
    """An oriented rectangle fitted around a point cloud on the ground."""

    center_lat: float
    center_lon: float
    length_m: float  # long side
    width_m: float  # short side
    heading_deg: float  # bearing of the long axis, folded into [0, 180)


def haversine_distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters between two WGS84 points."""
    latitude1_rad = math.radians(lat1)
    latitude2_rad = math.radians(lat2)
    delta_latitude_rad = math.radians(lat2 - lat1)
    delta_longitude_rad = math.radians(lon2 - lon1)

    haversine_term = (
        math.sin(delta_latitude_rad / 2.0) ** 2
        + math.cos(latitude1_rad) * math.cos(latitude2_rad)
        * math.sin(delta_longitude_rad / 2.0) ** 2
    )
    central_angle = 2.0 * math.asin(min(1.0, math.sqrt(haversine_term)))
    return EARTH_RADIUS_M * central_angle


def fold_heading_deg(heading_deg: float) -> float:
    """Fold an arbitrary bearing into the axis domain ``[0, 180)``."""
    return heading_deg % 180.0


def headings_equivalent(a_deg: float, b_deg: float,
                        tolerance_deg: float = SAME_FIELD_HEADING_TOLERANCE_DEG) -> bool:
    """True if two folded headings are within tolerance, honoring the
    wraparound (179° ≈ 1°). Callers pass raw bearings; 180° flips are
    equivalent by construction of the folded domain."""
    folded_a = fold_heading_deg(a_deg)
    folded_b = fold_heading_deg(b_deg)
    raw_difference = abs(folded_a - folded_b)
    circular_difference = min(raw_difference, 180.0 - raw_difference)
    return circular_difference <= tolerance_deg


def average_headings_deg(a_deg: float, a_weight: float,
                         b_deg: float, b_weight: float) -> float:
    """Weighted average of two folded headings, correct across the wrap
    (179° and 1° with equal weight average to 0°, never 90°).

    Implementation note: double the angles, average the unit vectors with the
    given weights, halve the resulting angle back into [0, 180).
    """
    doubled_a_rad = math.radians(2.0 * fold_heading_deg(a_deg))
    doubled_b_rad = math.radians(2.0 * fold_heading_deg(b_deg))

    vector_x = a_weight * math.cos(doubled_a_rad) + b_weight * math.cos(doubled_b_rad)
    vector_y = a_weight * math.sin(doubled_a_rad) + b_weight * math.sin(doubled_b_rad)

    averaged_doubled_rad = math.atan2(vector_y, vector_x)
    averaged_heading_deg = math.degrees(averaged_doubled_rad) / 2.0
    return fold_heading_deg(averaged_heading_deg)


def _cross_product_z(origin: Tuple[float, float],
                     first: Tuple[float, float],
                     second: Tuple[float, float]) -> float:
    """Z component of the cross product of vectors origin->first and
    origin->second. Positive means a counter-clockwise (left) turn."""
    return ((first[0] - origin[0]) * (second[1] - origin[1])
            - (first[1] - origin[1]) * (second[0] - origin[0]))


def convex_hull(points_xy: Sequence[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """Monotone-chain convex hull of local planar points, CCW order."""
    unique_points = sorted(set((float(x), float(y)) for x, y in points_xy))
    if len(unique_points) <= 2:
        return list(unique_points)

    lower_chain: List[Tuple[float, float]] = []
    for point in unique_points:
        while (len(lower_chain) >= 2
               and _cross_product_z(lower_chain[-2], lower_chain[-1], point) <= 0):
            lower_chain.pop()
        lower_chain.append(point)

    upper_chain: List[Tuple[float, float]] = []
    for point in reversed(unique_points):
        while (len(upper_chain) >= 2
               and _cross_product_z(upper_chain[-2], upper_chain[-1], point) <= 0):
            upper_chain.pop()
        upper_chain.append(point)

    # Concatenate, dropping the duplicated endpoints of each chain. The result
    # is counter-clockwise with no repeated final vertex.
    return lower_chain[:-1] + upper_chain[:-1]


def minimum_area_rect(points_xy: Sequence[Tuple[float, float]]
                      ) -> Tuple[Tuple[float, float], float, float, float]:
    """Rotating-calipers minimum-area oriented rectangle over a hull.

    Returns ``((center_x, center_y), long_side, short_side, axis_angle_rad)``
    where ``axis_angle_rad`` is the direction of the long side in the local
    plane (radians, math convention: CCW from +x/east).
    """
    hull = convex_hull(points_xy)
    if len(hull) < 3:
        raise ValueError("minimum_area_rect requires a hull with positive area")

    best_area = None
    best_result: Tuple[Tuple[float, float], float, float, float] = (
        (0.0, 0.0), 0.0, 0.0, 0.0)

    vertex_count = len(hull)
    for index in range(vertex_count):
        current_vertex = hull[index]
        next_vertex = hull[(index + 1) % vertex_count]

        edge_x = next_vertex[0] - current_vertex[0]
        edge_y = next_vertex[1] - current_vertex[1]
        edge_length = math.hypot(edge_x, edge_y)
        if edge_length == 0.0:
            continue

        # Orthonormal basis aligned with this hull edge.
        axis_x = edge_x / edge_length
        axis_y = edge_y / edge_length
        perpendicular_x = -axis_y
        perpendicular_y = axis_x

        min_along_edge = min_along_perpendicular = float("inf")
        max_along_edge = max_along_perpendicular = float("-inf")
        for point in hull:
            projection_edge = point[0] * axis_x + point[1] * axis_y
            projection_perpendicular = (point[0] * perpendicular_x
                                        + point[1] * perpendicular_y)
            min_along_edge = min(min_along_edge, projection_edge)
            max_along_edge = max(max_along_edge, projection_edge)
            min_along_perpendicular = min(min_along_perpendicular,
                                          projection_perpendicular)
            max_along_perpendicular = max(max_along_perpendicular,
                                          projection_perpendicular)

        extent_edge = max_along_edge - min_along_edge
        extent_perpendicular = max_along_perpendicular - min_along_perpendicular
        area = extent_edge * extent_perpendicular

        if best_area is not None and area >= best_area:
            continue
        best_area = area

        center_edge = (min_along_edge + max_along_edge) / 2.0
        center_perpendicular = (min_along_perpendicular
                                + max_along_perpendicular) / 2.0
        center_x = center_edge * axis_x + center_perpendicular * perpendicular_x
        center_y = center_edge * axis_y + center_perpendicular * perpendicular_y

        edge_angle = math.atan2(axis_y, axis_x)
        perpendicular_angle = math.atan2(perpendicular_y, perpendicular_x)
        if extent_edge >= extent_perpendicular:
            long_side = extent_edge
            short_side = extent_perpendicular
            axis_angle_rad = edge_angle
        else:
            long_side = extent_perpendicular
            short_side = extent_edge
            axis_angle_rad = perpendicular_angle

        best_result = ((center_x, center_y), long_side, short_side, axis_angle_rad)

    return best_result


def fit_rect_to_coordinates(coordinates: Sequence[Sequence[float]]) -> Optional[FittedRect]:
    """Fit an oriented rectangle to a cloud of ``[lat, lon]`` points.

    Projects to a local east/north plane around the centroid, takes the
    convex hull, fits the minimum-area rectangle, and converts back to a
    ``FittedRect`` with a folded compass heading.

    Returns ``None`` for degenerate input (fewer than 3 distinct points, or a
    collinear cloud with no area).
    """
    distinct_coordinates = set((float(lat), float(lon)) for lat, lon in coordinates)
    if len(distinct_coordinates) < 3:
        return None

    centroid_lat = sum(lat for lat, _ in distinct_coordinates) / len(distinct_coordinates)
    centroid_lon = sum(lon for _, lon in distinct_coordinates) / len(distinct_coordinates)
    cos_reference_latitude = math.cos(math.radians(centroid_lat))

    local_points = [
        (
            math.radians(lon - centroid_lon) * EARTH_RADIUS_M * cos_reference_latitude,
            math.radians(lat - centroid_lat) * EARTH_RADIUS_M,
        )
        for lat, lon in distinct_coordinates
    ]

    hull = convex_hull(local_points)
    if len(hull) < 3:
        return None

    (center_x, center_y), long_side, short_side, axis_angle_rad = minimum_area_rect(
        local_points)
    if long_side <= 0.0 or short_side <= 0.0:
        return None

    center_latitude = centroid_lat + math.degrees(center_y / EARTH_RADIUS_M)
    center_longitude = centroid_lon + math.degrees(
        center_x / (EARTH_RADIUS_M * cos_reference_latitude))

    heading_deg = fold_heading_deg(90.0 - math.degrees(axis_angle_rad))

    return FittedRect(
        center_lat=center_latitude,
        center_lon=center_longitude,
        length_m=long_side,
        width_m=short_side,
        heading_deg=heading_deg,
    )


def fit_track_as_field_observation(coordinates: Sequence[Sequence[float]]) -> Optional[FittedRect]:
    """Fit a match GPS track as evidence of field geometry.

    Applies ``MINIMUM_TRACK_POINTS_FOR_OBSERVATION``, expands the raw fit by
    ``TRACK_FIT_EXPANSION`` per axis, and returns ``None`` unless the expanded
    fit passes ``rect_within_sanity_bounds``.
    """
    if len(coordinates) < MINIMUM_TRACK_POINTS_FOR_OBSERVATION:
        return None

    raw_fit = fit_rect_to_coordinates(coordinates)
    if raw_fit is None:
        return None

    expanded_fit = FittedRect(
        center_lat=raw_fit.center_lat,
        center_lon=raw_fit.center_lon,
        length_m=raw_fit.length_m * TRACK_FIT_EXPANSION,
        width_m=raw_fit.width_m * TRACK_FIT_EXPANSION,
        heading_deg=raw_fit.heading_deg,
    )

    if not rect_within_sanity_bounds(expanded_fit):
        return None
    return expanded_fit


def rect_within_sanity_bounds(rect: FittedRect) -> bool:
    """True if the rectangle is a plausible soccer pitch."""
    minimum_length, maximum_length = FIELD_LENGTH_BOUNDS_M
    minimum_width, maximum_width = FIELD_WIDTH_BOUNDS_M
    return (
        minimum_length <= rect.length_m <= maximum_length
        and minimum_width <= rect.width_m <= maximum_width
    )


def rects_match_same_field(a: FittedRect, b: FittedRect) -> bool:
    """Spec proximity predicate: centers within 40 m, length/width within
    15 m, folded headings within 10° (180° flips equivalent)."""
    center_distance = haversine_distance_m(
        a.center_lat, a.center_lon, b.center_lat, b.center_lon)
    return (
        center_distance < SAME_FIELD_CENTER_DISTANCE_M
        and abs(a.length_m - b.length_m) < SAME_FIELD_LENGTH_TOLERANCE_M
        and abs(a.width_m - b.width_m) < SAME_FIELD_WIDTH_TOLERANCE_M
        and headings_equivalent(a.heading_deg, b.heading_deg,
                                SAME_FIELD_HEADING_TOLERANCE_DEG)
    )
