import math
from datetime import datetime

from flask import jsonify, request, abort, render_template
from marshmallow import ValidationError, EXCLUDE
from mongoengine.connection import get_connection

from match_tracks import app
from match_tracks import field_service, geometry
from match_tracks.auth import auth, current_principal, principal_may_read_team
from match_tracks.models import (Device, DeviceSchema, SessionSchema, FieldSchema,
                                 Match, Team, Player, CommunityField)
from match_tracks.rate_limit import rate_limited

device_schema = DeviceSchema()

# Meters per degree of latitude (matches tests/helpers.py convention). Used to
# convert a search radius in meters into a degree-space bounding box.
METERS_PER_DEGREE_LATITUDE = 111195.0


def _parse_timestamp(value):
    """Parse an incoming ISO-8601 timestamp string into a datetime.

    Python 3.9's ``datetime.fromisoformat`` does not accept a trailing 'Z', so
    strip it before parsing. Returns ``None`` for empty input.
    """
    if not value:
        return None
    text = str(value).strip()
    if text.endswith('Z'):
        text = text[:-1]
    return datetime.fromisoformat(text)


def _format_timestamp(value):
    """Serialize a datetime for NEW endpoint responses as ...Z, or ``None``."""
    if value is None:
        return None
    return value.strftime('%Y-%m-%dT%H:%M:%SZ')


def _track_coordinates(track):
    """Pull the raw ``[[lat, lon], ...]`` list out of a wire track dict."""
    if not track:
        return []
    return track.get('coordinates') or []


@app.route('/')
@app.route('/index/')
def index():
    user = {'username': 'Human'}
    return render_template('index.html', title='Home', user=user)


# HEALTH check
@app.route('/health', methods=['GET'])
def health():
    try:
        get_connection().admin.command('ping')
    except Exception:
        return jsonify({'status': 'degraded'}), 503
    return jsonify({'status': 'ok'}), 200


# READ all devices
@app.route('/devices/', methods=['GET'])
def get_devices():
    devices = Device.objects

    if devices.count() == 0:
        abort(404)

    result = device_schema.dump(devices, many=True)

    return jsonify({'devices': result})


# CREATE single device
@app.route('/devices/', methods=['POST'])
@auth.login_required
def create_device():
    json_data = request.get_json()

    if not json_data:
        return jsonify({'message': 'No input data provided.'}), 400

    # Validate and deserialize input
    try:
        created_device = device_schema.load(json_data)
    except ValidationError as err:
        return jsonify(err.messages), 422

    created_device.save()

    return jsonify({'device': created_device})


# READ single device
@app.route('/devices/<uuid:identifier>/', methods=['GET'])
def get_device(identifier):
    device = Device.objects(vendor_identifier=str(identifier)).first_or_404()

    result = device_schema.dump(device)

    return jsonify({'device': result})


# UPDATE single device by vendor_identifier
@app.route('/devices/<uuid:identifier>/', methods=['PUT'])
@auth.login_required
def update_device(identifier):
    device = Device.objects(vendor_identifier=str(identifier)).first_or_404()

    json_data = request.get_json()

    if not json_data:
        return jsonify({'result': 'No input data provided.'}), 400

    # Validate and deserialize input
    try:
        updated_device = device_schema.update(device, json_data)
    except ValidationError as err:
        return jsonify(err.messages), 422

    updated_device.save()

    return jsonify({'updated_device': device_schema.dump(updated_device)})


# DELETE single device
@app.route('/devices/<uuid:identifier>/', methods=['DELETE'])
@auth.login_required
def delete_device(identifier):
    device = Device.objects(vendor_identifier=str(identifier)).first_or_404()

    response = {'deleted_device': device_schema.dump(device)}

    try:
        device.delete()
    except Exception as err:
        return jsonify(type(err).__name__), 422

    return jsonify(response)


# READ device sessions (legacy, unauthenticated)
@app.route('/devices/<identifier>/sessions/', methods=['GET'])
def get_device_sessions(identifier):
    device = Device.objects(vendor_identifier=str(identifier)).first_or_404()

    result = SessionSchema().dump(device.sessions, many=True)

    return jsonify({'sessions': result})


# CREATE device sessions (legacy embedded write + extended Match dual-write)
@app.route('/devices/<identifier>/sessions/', methods=['POST'])
@auth.login_required
@rate_limited('write')
def add_session(identifier):
    json_data = request.get_json()['sessions']

    if not json_data:
        return jsonify({'result': 'No input data provided.'}), 400

    device = Device.objects(vendor_identifier=str(identifier)).first_or_404()

    # --- Legacy embedded write: unchanged response shape ---
    sessions = device.sessions

    try:
        new_sessions = SessionSchema().load(json_data, many=True, unknown=EXCLUDE)
    except ValidationError as err:
        return jsonify(err.messages), 422

    for session in new_sessions:
        sessions.append(session)

    sessions.save()
    result = SessionSchema().dump(new_sessions, many=True)

    # --- Extended dual-write into the top-level Match collection ---
    device_id = device.vendor_identifier.lower()
    for session_dict in json_data:
        _upsert_match_from_session(device_id, session_dict)

    return jsonify({'added_sessions': result})


def _upsert_match_from_session(device_id, session_dict):
    """Upsert a Match (and related Team/Player rows) from one session dict."""
    match_uuid = session_dict.get('uuid')
    if not match_uuid:
        return
    match_uuid = str(match_uuid).lower()

    existing_match = Match.objects(uuid=match_uuid).first()
    is_new = existing_match is None

    team_code = session_dict.get('team_code')
    player_name = session_dict.get('player_name')
    track = session_dict.get('track')
    provided_field_uuid = session_dict.get('field_uuid')

    # Resolve any provided field uuid (alias -> canonical) for the stored value.
    canonical_field = field_service.resolve_field_uuid(provided_field_uuid)
    field_uuid_value = canonical_field.uuid if canonical_field else None

    # Field observation ingest only on first insert (re-uploads must not
    # double-count observations).
    if is_new:
        ingested_field = field_service.ingest_track_observation(
            device_id, _track_coordinates(track), field_uuid=provided_field_uuid)
        if ingested_field is not None:
            field_uuid_value = ingested_field.uuid
    elif field_uuid_value is None:
        field_uuid_value = existing_match.field_uuid

    # Team: create a stub row when a code is referenced for the first time.
    if team_code and Team.objects(code=team_code).first() is None:
        Team(code=team_code, name=None).save()

    # Player: last-write-wins upsert keyed by device.
    if player_name is not None or team_code is not None:
        player = Player.objects(device_id=device_id).first() or Player(device_id=device_id)
        if player_name is not None:
            player.name = player_name
        if team_code is not None:
            player.team_code = team_code
        player.updated_at = datetime.utcnow()
        player.save()

    match = existing_match or Match(uuid=match_uuid)
    match.device_id = device_id
    match.field_uuid = field_uuid_value
    match.recorded_at = _parse_timestamp(session_dict.get('recorded_at'))
    match.duration_s = session_dict.get('duration_s')
    match.track = track
    match.events = session_dict.get('events') or []
    match.stats = session_dict.get('stats') or {}
    match.team_code = team_code
    match.save()


# READ device fields (legacy, unauthenticated)
@app.route('/devices/<identifier>/fields/', methods=['GET'])
def get_device_fields(identifier):
    device = Device.objects(vendor_identifier=str(identifier)).first_or_404()

    result = FieldSchema().dump(device.fields, many=True)

    return jsonify({'fields': result})


# CREATE device fields (legacy embedded write + community field ingest)
@app.route('/devices/<identifier>/fields/', methods=['POST'])
@auth.login_required
@rate_limited('write')
def add_fields(identifier):
    json_data = request.get_json()['fields']

    if not json_data:
        return jsonify({'result': 'No input data provided.'}), 400

    device = Device.objects(vendor_identifier=str(identifier)).first_or_404()

    # --- Legacy embedded write: unchanged response shape ---
    fields = device.fields

    try:
        new_fields = FieldSchema().load(json_data, many=True, unknown=EXCLUDE)
    except ValidationError as err:
        return jsonify(err.messages), 422

    for field in new_fields:
        fields.append(field)

    fields.save()
    result = FieldSchema().dump(new_fields, many=True)

    # --- Community field database ingest (dedup/merge/alias inside service) ---
    device_id = device.vendor_identifier.lower()
    for field_dict in json_data:
        field_uuid = field_dict.get('uuid')
        if not field_uuid:
            continue
        field_service.ingest_trained_field(
            device_id, str(field_uuid), _track_coordinates(field_dict.get('track')))

    return jsonify({'added_fields': result})


def _match_summary(match):
    """Compact list-view of a match (no track/events)."""
    return {
        'uuid': match.uuid,
        'recorded_at': _format_timestamp(match.recorded_at),
        'duration_s': match.duration_s,
        'field_uuid': match.field_uuid,
        'team_code': match.team_code,
        'stats': match.stats or {},
    }


def _match_detail(match):
    """Full record for a single match."""
    return {
        'uuid': match.uuid,
        'device_id': match.device_id,
        'recorded_at': _format_timestamp(match.recorded_at),
        'duration_s': match.duration_s,
        'field_uuid': match.field_uuid,
        'team_code': match.team_code,
        'track': match.track or {},
        'events': match.events or [],
        'stats': match.stats or {},
        'created_at': _format_timestamp(match.created_at),
    }


# LIST matches for a device (paginated, newest first)
@app.route('/devices/<identifier>/matches', methods=['GET'])
@auth.login_required
@rate_limited('read')
def get_device_matches(identifier):
    device_id = str(identifier).lower()

    limit = request.args.get('limit', default=20, type=int)
    if limit > 100:
        limit = 100
    if limit < 0:
        limit = 0
    offset = request.args.get('offset', default=0, type=int)
    if offset < 0:
        offset = 0

    query = Match.objects(device_id=device_id).order_by('-recorded_at')
    total = query.count()
    page = query.skip(offset).limit(limit)

    return jsonify({
        'matches': [_match_summary(match) for match in page],
        'total': total,
        'limit': limit,
        'offset': offset,
    })


# READ single match by uuid
@app.route('/devices/<identifier>/matches/<match_uuid>', methods=['GET'])
@auth.login_required
@rate_limited('read')
def get_device_match(identifier, match_uuid):
    device_id = str(identifier).lower()
    match = Match.objects(device_id=device_id, uuid=str(match_uuid).lower()).first()
    if match is None:
        abort(404)
    return jsonify({'match': _match_detail(match)})


# TEAM stats: per-player aggregates across all of a team's matches
@app.route('/teams/<code>/stats', methods=['GET'])
@auth.login_required
@rate_limited('read')
def get_team_stats(code):
    team = Team.objects(code=code).first()
    if team is None:
        abort(404)

    if not principal_may_read_team(current_principal(), code):
        abort(403)

    since = _parse_timestamp(request.args.get('since'))

    matches = Match.objects(team_code=code)
    if since is not None:
        matches = matches.filter(recorded_at__gte=since)

    aggregates_by_device = {}
    for match in matches:
        device_aggregate = aggregates_by_device.setdefault(match.device_id, {
            'device_id': match.device_id,
            'matches_played': 0,
            'total_minutes': 0.0,
            'total_distance_m': 0.0,
            'total_sprints': 0,
            'workrate_scores': [],
            'goals': 0,
            'assists': 0,
        })

        stats = match.stats or {}
        device_aggregate['matches_played'] += 1
        device_aggregate['total_minutes'] += (stats.get('time_on_pitch_s') or 0.0) / 60.0
        device_aggregate['total_distance_m'] += stats.get('total_distance_m') or 0.0
        device_aggregate['total_sprints'] += stats.get('sprint_count') or 0

        workrate_score = stats.get('workrate_score')
        if workrate_score is not None:
            device_aggregate['workrate_scores'].append(workrate_score)

        for event in match.events or []:
            kind = event.get('kind')
            if kind in ('goalFor', 'goalMine'):
                device_aggregate['goals'] += 1
            elif kind == 'assist':
                device_aggregate['assists'] += 1

    players = []
    for device_id, device_aggregate in aggregates_by_device.items():
        player = Player.objects(device_id=device_id).first()
        workrate_scores = device_aggregate.pop('workrate_scores')
        avg_workrate_score = (sum(workrate_scores) / len(workrate_scores)
                              if workrate_scores else None)
        device_aggregate['player_name'] = player.name if player else None
        device_aggregate['avg_workrate_score'] = avg_workrate_score
        players.append(device_aggregate)

    offset = request.args.get('offset', default=0, type=int)
    if offset < 0:
        offset = 0
    limit = request.args.get('limit', type=int)
    if limit is not None:
        players = players[offset:offset + limit]
    else:
        players = players[offset:]

    return jsonify({
        'team_code': code,
        'team_name': team.name,
        'players': players,
    })


# NEARBY community fields
@app.route('/fields/nearby', methods=['GET'])
@auth.login_required
@rate_limited('read')
def get_nearby_fields():
    latitude = request.args.get('lat', type=float)
    longitude = request.args.get('lon', type=float)
    if latitude is None or longitude is None:
        return jsonify({'message': 'lat and lon are required'}), 400

    radius_m = request.args.get('radius_m', default=2000.0, type=float)
    if radius_m > 20000.0:
        radius_m = 20000.0
    min_confidence = request.args.get('min_confidence', default=0.0, type=float)

    latitude_delta = radius_m / METERS_PER_DEGREE_LATITUDE
    cos_latitude = math.cos(math.radians(latitude))
    longitude_delta = (radius_m / (METERS_PER_DEGREE_LATITUDE * cos_latitude)
                       if cos_latitude else 180.0)

    candidates = CommunityField.objects(
        merged_into=None,
        rect_center_lat__gte=latitude - latitude_delta,
        rect_center_lat__lte=latitude + latitude_delta,
        rect_center_lon__gte=longitude - longitude_delta,
        rect_center_lon__lte=longitude + longitude_delta,
    )

    nearby = []
    for field in candidates:
        if field.rect_center_lat is None or field.rect_center_lon is None:
            continue
        distance_m = geometry.haversine_distance_m(
            latitude, longitude, field.rect_center_lat, field.rect_center_lon)
        if distance_m > radius_m:
            continue
        if (field.confidence or 0.0) < min_confidence:
            continue
        nearby.append((distance_m, field))

    nearby.sort(key=lambda item: item[0])

    fields = [{
        'uuid': field.uuid,
        'name': field.name,
        'source': field.source,
        'observation_count': field.observation_count,
        'confidence': field.confidence,
        'distance_m': distance_m,
        'outline': field.outline,
        'rectangle': {
            'center_lat': field.rect_center_lat,
            'center_lon': field.rect_center_lon,
            'length_m': field.rect_length_m,
            'width_m': field.rect_width_m,
            'heading_deg': field.rect_heading_deg,
        },
        'created_at': _format_timestamp(field.created_at),
    } for distance_m, field in nearby]

    return jsonify({'fields': fields})
