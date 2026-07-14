import math
from datetime import datetime

from flask import jsonify, request, abort, render_template
from marshmallow import ValidationError, EXCLUDE
from mongoengine.connection import get_connection

from match_tracks import app
from match_tracks import field_service, geometry
from match_tracks.auth import auth, current_principal, principal_may_read_team
from match_tracks.db import first_or_404
from match_tracks.models import (Device, DeviceSchema, SessionSchema, FieldSchema,
                                 Match, Team, Player, CommunityField)
from match_tracks.rate_limit import rate_limited

device_schema = DeviceSchema()

# Meters per degree of latitude (matches tests/helpers.py convention). Used to
# convert a search radius in meters into a degree-space bounding box.
METERS_PER_DEGREE_LATITUDE = 111195.0


def _normalized_device_id(identifier):
    """Canonical lowercase form of a device id.

    Clients generate their own UUIDs; Foundation's ``UUID.uuidString`` is
    uppercase while Python's ``uuid4()`` is lowercase, so every device lookup
    and write normalizes to lowercase to resolve to the same device regardless
    of the casing on the wire.
    """
    return str(identifier).strip().lower()


def _get_or_create_device(identifier):
    """Resolve the Device for ``identifier`` (case-insensitive), provisioning
    it on first write if absent.

    The watch/phone are offline-first: they mint a device UUID and start
    uploading with no separate registration call, so an unknown device is
    created here rather than 404'd. Tolerant of the unique-index race between
    two concurrent first uploads.
    """
    from mongoengine import NotUniqueError

    vendor_identifier = _normalized_device_id(identifier)
    device = Device.objects(vendor_identifier=vendor_identifier).first()
    if device is not None:
        return device
    try:
        device = Device(vendor_identifier=vendor_identifier)
        device.save()
        return device
    except NotUniqueError:
        return Device.objects(vendor_identifier=vendor_identifier).first()


def _ensure_team(team_code):
    """Create a stub Team row the first time a code is referenced, tolerating a
    concurrent create (two uploads for a brand-new team code at once)."""
    from mongoengine import NotUniqueError

    if not team_code or Team.objects(code=team_code).first() is not None:
        return
    try:
        Team(code=team_code, name=None).save()
    except NotUniqueError:
        pass  # another request created it first — fine


def _upsert_player(device_id, player_name, team_code):
    """Last-write-wins Player upsert keyed by device, safe against a concurrent
    first-insert. Without this, two simultaneous uploads for a new device both
    insert and the second hits the unique device_id index (500)."""
    from mongoengine import NotUniqueError

    if player_name is None and team_code is None:
        return
    for attempt in range(2):
        player = Player.objects(device_id=device_id).first() or Player(device_id=device_id)
        if player_name is not None:
            player.name = player_name
        if team_code is not None:
            player.team_code = team_code
        player.updated_at = datetime.utcnow()
        try:
            player.save()
            return
        except NotUniqueError:
            if attempt == 0:
                continue  # a concurrent insert won; retry as an update of the now-existing row
            raise


def _save_match_record(match_uuid, existing_match, field_values):
    """Save a Match idempotently by uuid, tolerating a concurrent insert of the
    same uuid (the client's retry queue may double-fire one session)."""
    from mongoengine import NotUniqueError

    match = existing_match or Match(uuid=match_uuid)
    for key, value in field_values.items():
        setattr(match, key, value)
    try:
        match.save()
        return
    except NotUniqueError:
        pass
    # The concurrent insert won; reload the winning row and apply our values.
    match = Match.objects(uuid=match_uuid).first()
    if match is None:
        raise RuntimeError('match vanished after duplicate-key on insert')
    for key, value in field_values.items():
        setattr(match, key, value)
    match.save()


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

    # Normalize the client-generated id to lowercase (see _normalized_device_id).
    if created_device.vendor_identifier:
        created_device.vendor_identifier = created_device.vendor_identifier.lower()

    # Idempotent registration: a device may already exist because a session or
    # field upload auto-provisioned it. Update rather than fail the unique index.
    existing = Device.objects(vendor_identifier=created_device.vendor_identifier).first()
    if existing is not None:
        if created_device.name is not None:
            existing.name = created_device.name
            existing.save()
        return jsonify({'device': device_schema.dump(existing)})

    created_device.save()

    return jsonify({'device': device_schema.dump(created_device)})


# READ single device
@app.route('/devices/<uuid:identifier>/', methods=['GET'])
def get_device(identifier):
    device = first_or_404(Device.objects(vendor_identifier=str(identifier)))

    result = device_schema.dump(device)

    return jsonify({'device': result})


# UPDATE single device by vendor_identifier
@app.route('/devices/<uuid:identifier>/', methods=['PUT'])
@auth.login_required
def update_device(identifier):
    device = first_or_404(Device.objects(vendor_identifier=str(identifier)))

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


# DELETE single device — replaced in V2 by the compliance cascade in
# match_tracks/privacy.py (DELETE /devices/<device_id> → 202 + deletion_id),
# which also removes the legacy Device document. The old route lived here.


# READ device sessions (legacy, unauthenticated)
@app.route('/devices/<identifier>/sessions/', methods=['GET'])
def get_device_sessions(identifier):
    device = first_or_404(Device.objects(
        vendor_identifier=_normalized_device_id(identifier)))

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

    # V2 hardening: a device key may only upload sessions as itself (admin
    # keys may act for any device) — otherwise one device could fabricate
    # matches, player state, and team auto-joins for another.
    from match_tracks.memberships import actor_denial
    denial = actor_denial(identifier)
    if denial:
        return denial

    device = _get_or_create_device(identifier)
    device_id = device.vendor_identifier.lower()

    # --- V2: the uploading device must belong to any team it tags (§6) ---
    membership_denial = _validate_session_team_memberships(device_id, json_data)
    if membership_denial:
        return membership_denial

    # --- Legacy embedded write: unchanged response shape ---
    sessions = device.sessions

    try:
        new_sessions = SessionSchema().load(json_data, many=True, unknown=EXCLUDE)
    except ValidationError as err:
        return jsonify(err.messages), 422

    # V2 idempotency: a session uuid that already exists as a Match has been
    # ingested before — do not append a duplicate embedded copy on retry.
    existing_embedded_uuids = {
        embedded.uuid for embedded in sessions if getattr(embedded, 'uuid', None)}
    for session in new_sessions:
        session_uuid = (session.uuid or '').lower() or None
        session.uuid = session_uuid
        already_ingested = session_uuid and (
            session_uuid in existing_embedded_uuids
            or Match.objects(uuid=session_uuid).first() is not None)
        if not already_ingested:
            sessions.append(session)

    sessions.save()
    result = SessionSchema().dump(new_sessions, many=True)

    # --- Extended dual-write into the top-level Match collection ---
    for session_dict in json_data:
        _upsert_match_from_session(device_id, session_dict)

    return jsonify({'added_sessions': result})


def _validate_session_team_memberships(device_id, session_dicts):
    """400 when the device tags a team it doesn't belong to (V2 §6).

    Admin keys bypass. A device with no memberships anywhere (no rows, no
    legacy default team) is auto-joined to the tagged team — smooth V1→V2
    migration for solo users.
    """
    from mongoengine import NotUniqueError

    from match_tracks.memberships import is_member
    from match_tracks.models import DeviceTeamMembership

    principal = current_principal()
    if principal and principal.get('admin'):
        return None

    # Evaluate the whole batch against the pre-request state first, so a
    # denial partway through never leaves an auto-join half-applied.
    has_any_membership = (
        DeviceTeamMembership.objects(device_id=device_id).first() is not None
        or Player.objects(device_id=device_id, team_code__ne=None).first() is not None)

    teams_to_join = []
    for session_dict in session_dicts:
        team_code = session_dict.get('team_code')
        if not team_code or is_member(device_id, team_code):
            continue
        if has_any_membership:
            return jsonify({'reason': 'not_a_member'}), 400
        if team_code not in teams_to_join:
            teams_to_join.append(team_code)

    for team_code in teams_to_join:
        _ensure_team(team_code)
        try:
            DeviceTeamMembership(device_id=device_id, team_code=team_code).save()
        except NotUniqueError:
            pass  # a concurrent auto-join for the same (device, team) won — fine

    return None


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

    sport_id = session_dict.get('sport_id')

    # Field observation ingest only on first insert (re-uploads must not
    # double-count observations).
    if is_new:
        ingested_field = field_service.ingest_track_observation(
            device_id, _track_coordinates(track), field_uuid=provided_field_uuid,
            sport_id=sport_id)
        if ingested_field is not None:
            field_uuid_value = ingested_field.uuid
    elif field_uuid_value is None:
        field_uuid_value = existing_match.field_uuid

    # Team stub + last-write-wins Player upsert (both concurrency-safe).
    _ensure_team(team_code)
    _upsert_player(device_id, player_name, team_code)

    from match_tracks.sports_config import normalized_sport_id

    _save_match_record(match_uuid, existing_match, dict(
        device_id=device_id,
        field_uuid=field_uuid_value,
        recorded_at=_parse_timestamp(session_dict.get('recorded_at')),
        duration_s=session_dict.get('duration_s'),
        track=track,
        events=session_dict.get('events') or [],
        stats=session_dict.get('stats') or {},
        team_code=team_code,
        sport_id=normalized_sport_id(sport_id),
    ))


# READ device fields (legacy, unauthenticated)
@app.route('/devices/<identifier>/fields/', methods=['GET'])
def get_device_fields(identifier):
    device = first_or_404(Device.objects(
        vendor_identifier=_normalized_device_id(identifier)))

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

    from match_tracks.memberships import actor_denial
    denial = actor_denial(identifier)
    if denial:
        return denial

    device = _get_or_create_device(identifier)

    # --- Legacy embedded write: unchanged response shape ---
    fields = device.fields

    try:
        new_fields = FieldSchema().load(json_data, many=True, unknown=EXCLUDE)
    except ValidationError as err:
        return jsonify(err.messages), 422

    # V2 idempotency: a field uuid that already resolves in the community
    # database has been ingested before — skip the duplicate embedded copy.
    existing_embedded_uuids = {
        embedded.uuid for embedded in fields if getattr(embedded, 'uuid', None)}
    for field in new_fields:
        field_uuid = (field.uuid or '').lower() or None
        field.uuid = field_uuid
        already_ingested = field_uuid and (
            field_uuid in existing_embedded_uuids
            or field_service.resolve_field_uuid(field_uuid) is not None)
        if not already_ingested:
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
            device_id, str(field_uuid), _track_coordinates(field_dict.get('track')),
            sport_id=field_dict.get('sport_id'))

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
    # V2 hardening: raw match data (full tracks) is the device's own; only the
    # device itself or an admin may read it.
    from match_tracks.memberships import actor_denial
    denial = actor_denial(identifier)
    if denial:
        return denial

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
    from match_tracks.memberships import actor_denial
    denial = actor_denial(identifier)
    if denial:
        return denial

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

    from match_tracks.privacy import consent_blocked, player_display_name

    players = []
    for device_id, device_aggregate in aggregates_by_device.items():
        player = Player.objects(device_id=device_id).first()
        # V2 §5: teams flagged requires_consent hide players until a guardian
        # has acknowledged consent.
        if consent_blocked(player, team):
            continue
        workrate_scores = device_aggregate.pop('workrate_scores')
        avg_workrate_score = (sum(workrate_scores) / len(workrate_scores)
                              if workrate_scores else None)
        device_aggregate['player_name'] = player_display_name(player)
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

    from match_tracks.sports_config import normalized_sport_id, sports_match
    sport_filter_given = 'sport_id' in request.args
    sport_filter = normalized_sport_id(request.args.get('sport_id'))

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
        if sport_filter_given and not sports_match(field.sport_id, sport_filter):
            continue
        # Satellite-seeded fields with no real observation stay hidden unless
        # the caller opts into low confidence (V2 §8).
        if field.seeded and (field.observation_count or 0) == 0 and min_confidence > 0.25:
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
