"""Test utilities: synthetic GPS data generators and wire-payload builders.

Shared by tests/test_legacy_compat.py and tests/test_api.py. Nothing here
talks to the app directly except ``register_device``, which is a thin
convenience wrapper around the (pre-existing) ``POST /devices/`` endpoint.

Geometry conventions match match_tracks/geometry.py:
- Coordinates are ``[lat, lon]`` pairs (the wire format used by clients).
- Headings are compass bearings in degrees; 0 means the field's long axis
  points north-south.
- All distance math is equirectangular (flat-earth approximation), which is
  accurate enough at soccer-pitch scale: meters per degree of latitude is
  treated as a constant, and meters per degree of longitude is scaled by
  cos(latitude).
"""

import math
import random
import uuid as uuid_module

METERS_PER_DEGREE_LATITUDE = 111195.0


def meters_per_degree_longitude(latitude_degrees):
    """Meters per degree of longitude at the given latitude."""
    return METERS_PER_DEGREE_LATITUDE * math.cos(math.radians(latitude_degrees))


def offset_coordinate(latitude, longitude, east_offset_m=0.0, north_offset_m=0.0):
    """Return a ``[lat, lon]`` pair offset from the given point by meters."""
    offset_latitude = latitude + north_offset_m / METERS_PER_DEGREE_LATITUDE
    offset_longitude = longitude + east_offset_m / meters_per_degree_longitude(latitude)
    return [offset_latitude, offset_longitude]


def _rotate_along_across_to_east_north(along_offset_m, across_offset_m, heading_deg):
    """Rotate local (along long axis, across long axis) offsets to (east, north).

    ``heading_deg`` is the compass bearing of the long axis (0 = north-south).
    """
    heading_rad = math.radians(heading_deg)
    long_axis_east = math.sin(heading_rad)
    long_axis_north = math.cos(heading_rad)
    short_axis_east = math.cos(heading_rad)
    short_axis_north = -math.sin(heading_rad)

    east_offset_m = along_offset_m * long_axis_east + across_offset_m * short_axis_east
    north_offset_m = along_offset_m * long_axis_north + across_offset_m * short_axis_north
    return east_offset_m, north_offset_m


def make_field_outline(center_lat, center_lon, length_m=100.0, width_m=64.0,
                        heading_deg=0.0, points_per_side=12):
    """Build the perimeter of an oriented rectangle as ``[lat, lon]`` points.

    Simulates a device walking the boundary of a trained field: points march
    around all four sides in order, ``points_per_side`` per side.
    """
    half_length_m = length_m / 2.0
    half_width_m = width_m / 2.0

    corners_along_across = [
        (-half_length_m, -half_width_m),
        (half_length_m, -half_width_m),
        (half_length_m, half_width_m),
        (-half_length_m, half_width_m),
    ]

    outline_points = []
    for corner_index in range(4):
        start_along, start_across = corners_along_across[corner_index]
        end_along, end_across = corners_along_across[(corner_index + 1) % 4]
        for step_index in range(points_per_side):
            fraction = step_index / points_per_side
            along_offset_m = start_along + (end_along - start_along) * fraction
            across_offset_m = start_across + (end_across - start_across) * fraction
            east_offset_m, north_offset_m = _rotate_along_across_to_east_north(
                along_offset_m, across_offset_m, heading_deg)
            outline_points.append(
                offset_coordinate(center_lat, center_lon, east_offset_m, north_offset_m))

    return outline_points


def make_match_track(center_lat, center_lon, length_m=100.0, width_m=64.0,
                      heading_deg=0.0, num_points=400, seed=42):
    """Build a pseudo-random point cloud covering a field's interior.

    Simulates the GPS breadcrumb trail of a player moving around a match, so
    that an oriented-rectangle fit recovers dimensions close to
    ``length_m``/``width_m``. Points are spread uniformly across ~97% of each
    axis (nearly edge-to-edge, but never quite touching the boundary, the way
    a real player track under-covers the pitch).
    """
    random_generator = random.Random(seed)
    coverage_fraction = 0.97
    half_length_m = (length_m * coverage_fraction) / 2.0
    half_width_m = (width_m * coverage_fraction) / 2.0

    track_points = []
    for _ in range(num_points):
        along_offset_m = random_generator.uniform(-half_length_m, half_length_m)
        across_offset_m = random_generator.uniform(-half_width_m, half_width_m)
        east_offset_m, north_offset_m = _rotate_along_across_to_east_north(
            along_offset_m, across_offset_m, heading_deg)
        track_points.append(
            offset_coordinate(center_lat, center_lon, east_offset_m, north_offset_m))

    return track_points


def new_uuid():
    """A fresh lowercase UUID string, suitable for any ``uuid`` field on the wire."""
    return str(uuid_module.uuid4())


def register_device(client, admin_headers, vendor_identifier=None, name='Test Device'):
    """Create a Device row via ``POST /devices/`` using the admin API key.

    Returns the ``vendor_identifier`` (generating one if not given), which is
    the identifier used in every ``/devices/{uuid}/...`` route.
    """
    if vendor_identifier is None:
        vendor_identifier = new_uuid()

    response = client.post(
        '/devices/',
        json={'vendor_identifier': vendor_identifier, 'name': name},
        headers=admin_headers,
    )
    assert response.status_code in (200, 201), (
        f"failed to create device {vendor_identifier}: "
        f"{response.status_code} {response.get_data(as_text=True)}"
    )
    return vendor_identifier


def make_session_payload(session_uuid, recorded_at, coordinates, **additional_fields):
    """Build a single legacy session wire object.

    ``additional_fields`` lets callers tack on arbitrary extra keys (to test
    that unknown keys are tolerated) without needing the extended-session
    vocabulary.
    """
    payload = {
        'uuid': session_uuid,
        'recorded_at': recorded_at,
        'track': {'coordinates': coordinates},
    }
    payload.update(additional_fields)
    return payload


def make_extended_session_payload(session_uuid, recorded_at, coordinates, *,
                                   duration_s=None, field_uuid=None, team_code=None,
                                   player_name=None, events=None, stats=None,
                                   **additional_fields):
    """Build a session wire object using the additive extended-session keys.

    Only keys explicitly passed (non-``None``) are included, so callers can
    build minimal or fully-populated payloads as needed.
    """
    payload = make_session_payload(session_uuid, recorded_at, coordinates)

    if duration_s is not None:
        payload['duration_s'] = duration_s
    if field_uuid is not None:
        payload['field_uuid'] = field_uuid
    if team_code is not None:
        payload['team_code'] = team_code
    if player_name is not None:
        payload['player_name'] = player_name
    if events is not None:
        payload['events'] = events
    if stats is not None:
        payload['stats'] = stats

    payload.update(additional_fields)
    return payload


def make_field_payload(field_uuid, recorded_at, coordinates, **additional_fields):
    """Build a single field-training wire object (trained outline upload).

    The outline coordinates go in ``track``, mirroring the legacy session
    shape.
    """
    payload = {
        'uuid': field_uuid,
        'recorded_at': recorded_at,
        'track': {'coordinates': coordinates},
    }
    payload.update(additional_fields)
    return payload


def make_event(kind, date, event_uuid=None, note=''):
    """Build a single match event object: ``{'uuid', 'kind', 'date', 'note'}``."""
    return {
        'uuid': event_uuid or new_uuid(),
        'kind': kind,
        'date': date,
        'note': note,
    }


def make_match_stats(total_distance_m=0.0, time_on_pitch_s=0.0, sprint_count=0,
                      run_count=0, workrate_score=0.0, speed_zones=None,
                      avg_hr=None, position_role=None, position_side=None):
    """Build a match ``stats`` wire dict with sensible zeroed defaults."""
    if speed_zones is None:
        speed_zones = {
            'standing_s': 0.0,
            'walking_s': 0.0,
            'jogging_s': 0.0,
            'running_s': 0.0,
            'sprinting_s': 0.0,
        }

    stats = {
        'total_distance_m': total_distance_m,
        'time_on_pitch_s': time_on_pitch_s,
        'sprint_count': sprint_count,
        'run_count': run_count,
        'workrate_score': workrate_score,
        'speed_zones': speed_zones,
    }
    if avg_hr is not None:
        stats['avg_hr'] = avg_hr
    if position_role is not None:
        stats['position_role'] = position_role
    if position_side is not None:
        stats['position_side'] = position_side
    return stats
