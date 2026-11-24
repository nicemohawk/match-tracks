"""Community field seeding from satellite/aerial imagery.

Devices can ask the server to look for pitches near a point when the
crowd-sourced field database (``match_tracks.field_service``) has nothing
there yet. A seed request is queued (``POST /fields/seed-request``) and later
processed by :func:`run_pending_seed_jobs` (invoked by the coordinator-owned
CLI wrapper ``scripts/run_seed_jobs.py``), which asks the configured imagery
provider (``match_tracks.imagery``) for a white-line detection mask, looks
for pitch-shaped connected components in it, and inserts new canonical
``CommunityField`` rows — unconfirmed until a real observation merges into
them (see ``docs/backend-v2-architecture.md`` §8).
"""

import os
from collections import deque
from datetime import timedelta
from uuid import uuid4

from flask import Blueprint, Response, current_app, jsonify, request

from match_tracks import field_service, sports_config
from match_tracks.auth import auth, current_principal, effective_device_id
from match_tracks.geometry import FittedRect
from match_tracks.imagery import get_imagery_provider, grid_cell_center
from match_tracks.models import CommunityField, SeedRequest
from match_tracks.rate_limit import rate_limited
from match_tracks.timeutils import utcnow

seeding_blueprint = Blueprint('seeding', __name__)

SEED_REQUEST_DEFAULT_RADIUS_M = 1500.0
SEED_REQUEST_MINIMUM_INTERVAL = timedelta(hours=24)
IMAGERY_CACHE_MAX_AGE_SECONDS = 2592000  # 30 days


@seeding_blueprint.route('/fields/seed-request', methods=['POST'])
@auth.login_required
@rate_limited('write')
def create_seed_request():
    """Queue a satellite-imagery scan for pitches near a point.

    Body: ``{"lat", "lon", "radius_m"?}``. Limited to one request per 24
    hours per identity (the requesting device, or the admin key string when
    an admin principal supplies no ``X-Device-ID``).
    """
    json_data = request.get_json(silent=True) or {}
    try:
        latitude = float(json_data['lat'])
        longitude = float(json_data['lon'])
    except (KeyError, TypeError, ValueError):
        return jsonify({'reason': 'lat_lon_required'}), 400

    radius_m = float(json_data.get('radius_m') or SEED_REQUEST_DEFAULT_RADIUS_M)

    identity = _requester_identity()

    cutoff = utcnow() - SEED_REQUEST_MINIMUM_INTERVAL
    recent_request = SeedRequest.objects(
        device_id=identity, requested_at__gt=cutoff).first()
    if recent_request is not None:
        return jsonify({'reason': 'seed_request_daily_limit'}), 429

    seed_request = SeedRequest(
        uuid=str(uuid4()),
        device_id=identity,
        latitude=latitude,
        longitude=longitude,
        radius_m=radius_m,
        status='pending',
    )
    seed_request.save()
    return jsonify({'request_id': seed_request.uuid}), 202


@seeding_blueprint.route('/fields/<field_uuid>/imagery', methods=['GET'])
@auth.login_required
@rate_limited('read')
def get_field_imagery(field_uuid):
    """Return a cached (or freshly captured) satellite snapshot for a field."""
    cache_directory = current_app.config.get('IMAGERY_CACHE_DIR') or os.path.join(
        current_app.instance_path, 'imagery')
    os.makedirs(cache_directory, exist_ok=True)

    cache_path = os.path.join(cache_directory, f'{field_uuid.lower()}.png')
    if os.path.exists(cache_path):
        with open(cache_path, 'rb') as cached_file:
            return _imagery_response(cached_file.read())

    field = field_service.resolve_field_uuid(field_uuid)
    if field is None:
        return jsonify({'reason': 'unknown_field'}), 404

    provider = get_imagery_provider()
    span_m = max(field.rect_length_m or 0, field.rect_width_m or 0) * 1.5
    png_bytes = provider.snapshot_png(field.rect_center_lat, field.rect_center_lon, span_m=span_m)
    if png_bytes is None:
        return jsonify({'reason': 'imagery_unavailable'}), 404

    with open(cache_path, 'wb') as cache_file:
        cache_file.write(png_bytes)

    return _imagery_response(png_bytes)


def _requester_identity():
    """The rate-limiting/ownership identity for a seed request.

    The requesting device (``effective_device_id()``), or — when an admin
    principal supplied no ``X-Device-ID`` header — the admin's api key string.
    """
    identity = effective_device_id()
    if identity is not None:
        return identity

    principal = current_principal()
    if principal is not None:
        return principal.get('key')
    return None


def _imagery_response(png_bytes):
    response = Response(png_bytes, mimetype='image/png')
    response.headers['Cache-Control'] = f'max-age={IMAGERY_CACHE_MAX_AGE_SECONDS}'
    return response


def run_pending_seed_jobs(provider=None):
    """Process every pending :class:`SeedRequest`, inserting seeded fields.

    For each pending request, asks ``provider`` (or the app-configured
    provider) for a line mask; a request whose provider returns no mask is
    marked ``failed``. Otherwise every detected pitch rectangle that does not
    already match an existing canonical field becomes a new, unconfirmed
    ``CommunityField`` (``seeded=True``, ``confidence=0.25``,
    ``observation_count=0``); the request is then marked ``completed``.

    Returns ``{'processed': <int>, 'fields_created': <int>}``.
    """
    if provider is None:
        provider = get_imagery_provider()

    processed = 0
    fields_created = 0

    for seed_request in SeedRequest.objects(status='pending'):
        processed += 1

        mask = provider.line_mask(
            seed_request.latitude, seed_request.longitude, seed_request.radius_m)
        if mask is None:
            seed_request.status = 'failed'
            seed_request.save()
            continue

        for rectangle in detect_pitch_rectangles(mask):
            fitted_rect = FittedRect(
                center_lat=rectangle['center_lat'],
                center_lon=rectangle['center_lon'],
                length_m=rectangle['length_m'],
                width_m=rectangle['width_m'],
                heading_deg=rectangle['heading_deg'],
            )
            if field_service.find_matching_canonical_field(fitted_rect) is not None:
                continue

            CommunityField(
                uuid=str(uuid4()),
                source='community',
                confidence=0.25,
                observation_count=0,
                seeded=True,
                has_trained_observation=False,
                outline=None,
                sport_id=rectangle['sport_id'],
                contributing_device_ids=[],
                rect_center_lat=fitted_rect.center_lat,
                rect_center_lon=fitted_rect.center_lon,
                rect_length_m=fitted_rect.length_m,
                rect_width_m=fitted_rect.width_m,
                rect_heading_deg=fitted_rect.heading_deg,
            ).save()
            fields_created += 1

        seed_request.status = 'completed'
        seed_request.save()

    return {'processed': processed, 'fields_created': fields_created}


def detect_pitch_rectangles(mask):
    """Find pitch-shaped connected components in a provider line mask.

    ``mask`` follows the ``imagery`` provider protocol: a boolean ``grid``
    (row 0 northernmost, column 0 westernmost), ``meters_per_cell``,
    ``origin_lat``, ``origin_lon``. Each 4-connected component of ``True``
    cells is reduced to its bounding box and kept only when the resulting
    (length, width) fits a known sport's plausible pitch dimensions (soccer
    checked first, then every other configured sport profile).

    Returns a list of dicts: ``{'center_lat', 'center_lon', 'length_m',
    'width_m', 'heading_deg', 'sport_id'}`` (``sport_id`` is ``None`` for a
    soccer-shaped pitch).
    """
    grid = mask['grid']
    meters_per_cell = mask['meters_per_cell']
    origin_lat = mask['origin_lat']
    origin_lon = mask['origin_lon']

    row_count = len(grid)
    column_count = len(grid[0]) if row_count else 0
    visited = [[False] * column_count for _ in range(row_count)]

    rectangles = []
    for start_row in range(row_count):
        for start_column in range(column_count):
            if not grid[start_row][start_column] or visited[start_row][start_column]:
                continue

            component_cells = _connected_component(
                grid, visited, start_row, start_column, row_count, column_count)
            rectangle = _rectangle_for_component(
                component_cells, meters_per_cell, origin_lat, origin_lon)
            if rectangle is not None:
                rectangles.append(rectangle)

    return rectangles


def _connected_component(grid, visited, start_row, start_column, row_count, column_count):
    """Iterative 4-connected breadth-first search collecting one component's cells."""
    visited[start_row][start_column] = True
    pending_cells = deque([(start_row, start_column)])
    component_cells = []

    while pending_cells:
        row, column = pending_cells.popleft()
        component_cells.append((row, column))

        for neighbor_row, neighbor_column in (
            (row - 1, column), (row + 1, column),
            (row, column - 1), (row, column + 1),
        ):
            if (0 <= neighbor_row < row_count and 0 <= neighbor_column < column_count
                    and grid[neighbor_row][neighbor_column]
                    and not visited[neighbor_row][neighbor_column]):
                visited[neighbor_row][neighbor_column] = True
                pending_cells.append((neighbor_row, neighbor_column))

    return component_cells


def _rectangle_for_component(component_cells, meters_per_cell, origin_lat, origin_lon):
    """Bounding-box rectangle for a component, or ``None`` if implausibly sized."""
    rows = [row for row, _ in component_cells]
    columns = [column for _, column in component_cells]
    min_row, max_row = min(rows), max(rows)
    min_column, max_column = min(columns), max(columns)

    row_span = max_row - min_row + 1
    column_span = max_column - min_column + 1
    height_m = row_span * meters_per_cell
    width_m = column_span * meters_per_cell

    length_m = max(height_m, width_m)
    short_side_m = min(height_m, width_m)
    heading_deg = 0.0 if row_span > column_span else 90.0

    sport_id, fits_a_profile = _sport_id_for_dimensions(length_m, short_side_m)
    if not fits_a_profile:
        return None

    center_row = (min_row + max_row) / 2.0
    center_column = (min_column + max_column) / 2.0
    center_lat, center_lon = grid_cell_center(
        origin_lat, origin_lon, meters_per_cell, center_row, center_column)

    return {
        'center_lat': center_lat,
        'center_lon': center_lon,
        'length_m': length_m,
        'width_m': short_side_m,
        'heading_deg': heading_deg,
        'sport_id': sport_id,
    }


def _sport_id_for_dimensions(length_m, width_m):
    """The best-matching sport id for a (length, width), soccer preferred.

    Returns ``(sport_id, matched)``: ``matched`` is ``False`` when no
    configured sport's plausibility bounds accept these dimensions.
    ``sport_id`` is ``None`` for a soccer-shaped pitch (per
    ``CommunityField.sport_id`` — ``None`` means soccer).
    """
    if _fits_bounds(length_m, width_m, sports_config.plausibility_bounds('soccer')):
        return None, True

    for profile in sports_config.all_profiles():
        profile_sport_id = _profile_sport_id(profile)
        if profile_sport_id is None or profile_sport_id == 'soccer':
            continue
        if _fits_bounds(length_m, width_m, sports_config.plausibility_bounds(profile_sport_id)):
            return profile_sport_id, True

    return None, False


def _profile_sport_id(profile):
    """Extract a sport id from whatever ``sports_config.all_profiles()`` yields."""
    if isinstance(profile, str):
        return profile
    if isinstance(profile, dict):
        return profile.get('sport_id')
    return getattr(profile, 'sport_id', None)


def _fits_bounds(length_m, width_m, bounds):
    (min_length, max_length), (min_width, max_width) = bounds
    return min_length <= length_m <= max_length and min_width <= width_m <= max_width
