"""Live match telemetry (V2 §1).

Each device posts a running snapshot of its live match to
``POST /devices/<device_id>/live``; the server keeps exactly one row per
device (``LiveStatus``, primary key ``device_id``) and overwrites it on every
accepted update. Teammates poll ``GET /teams/<code>/live`` for the roster's
latest positions. Updates are sequence-guarded so an out-of-order or replayed
snapshot never clobbers a newer one.
"""

from datetime import timedelta, timezone

from flask import Blueprint, jsonify, request

from match_tracks import entitlements, memberships
from match_tracks.auth import auth
from match_tracks.models import LiveStatus, Player, Team
from match_tracks.privacy import consent_blocked, player_display_name
from match_tracks.rate_limit import rate_limited
from match_tracks.timeutils import parse_iso, utcnow

live_blueprint = Blueprint('live', __name__)

TIMESTAMP_FORMAT = '%Y-%m-%dT%H:%M:%SZ'
STALE_AFTER_SECONDS = 30


def _parse_timestamp(value):
    """Parse an incoming ISO-8601 timestamp to an aware UTC datetime.

    Tolerates a trailing 'Z' or an offset; a naive input is assumed UTC.
    Returns ``None`` for empty or unparseable input.
    """
    try:
        return parse_iso(value)
    except ValueError:
        return None


def _format_timestamp(value):
    """Serialize a datetime as ``...Z`` (UTC, second precision), or ``None``.

    Normalizes to UTC first so a non-UTC-aware value can never mis-serialize.
    """
    if value is None:
        return None
    return value.astimezone(timezone.utc).strftime(TIMESTAMP_FORMAT)


# POST a live snapshot for one device
@live_blueprint.route('/devices/<device_id>/live', methods=['POST'])
@auth.login_required
@rate_limited('live')
def post_live_status(device_id):
    """Store this device's latest live-match snapshot, sequence-guarded.

    A snapshot for the SAME match whose sequence is not newer than the stored
    one is ignored (200 ``{"stale": true}``); anything else overwrites the
    device's entire row (204). A new match_uuid is always accepted regardless
    of the previously stored sequence.
    """
    denial = memberships.actor_denial(device_id)
    if denial:
        return denial

    entitlement_denial = entitlements.require_team_entitlement()
    if entitlement_denial:
        return entitlement_denial

    json_data = request.get_json(silent=True) or {}
    team_code = json_data.get('team_code')

    member_denial = memberships.require_member(team_code)
    if member_denial:
        return member_denial

    normalized_device_id = str(device_id).lower()
    match_uuid = json_data.get('match_uuid')
    normalized_match_uuid = str(match_uuid).lower() if match_uuid else None

    try:
        incoming_sequence = int(json_data.get('sequence') or 0)
    except (TypeError, ValueError):
        incoming_sequence = 0

    existing = LiveStatus.objects(device_id=normalized_device_id).first()
    stored_match_uuid = (existing.match_uuid.lower()
                         if existing and existing.match_uuid else None)
    if (existing is not None
            and stored_match_uuid == normalized_match_uuid
            and (existing.sequence or 0) >= incoming_sequence):
        return jsonify({'stale': True}), 200

    updated_at = _parse_timestamp(json_data.get('timestamp')) or utcnow()

    live_status = LiveStatus(device_id=normalized_device_id)
    live_status.team_code = team_code
    live_status.match_uuid = normalized_match_uuid
    live_status.sequence = incoming_sequence
    live_status.updated_at = updated_at
    live_status.elapsed_s = json_data.get('elapsed_s')
    live_status.heart_rate = json_data.get('heart_rate')
    live_status.distance_m = json_data.get('distance_m')
    live_status.current_speed = json_data.get('current_speed')
    live_status.on_pitch = bool(json_data.get('on_pitch', True))
    live_status.us_goals = json_data.get('us_goals')
    live_status.them_goals = json_data.get('them_goals')
    live_status.x = json_data.get('x')
    live_status.y = json_data.get('y')
    live_status.save()

    return '', 204


# GET the live roster for a team
@live_blueprint.route('/teams/<code>/live', methods=['GET'])
@auth.login_required
@rate_limited('read')
def get_team_live(code):
    """Latest live snapshot for every device currently on the team.

    Players whose team gates rosters behind guardian consent (and who have not
    consented) are excluded. Each entry is flagged ``stale`` when its snapshot
    is older than 30 seconds.
    """
    member_denial = memberships.require_member(code)
    if member_denial:
        return member_denial

    team = Team.objects(code=code).first()
    now = utcnow()
    stale_cutoff = now - timedelta(seconds=STALE_AFTER_SECONDS)

    players = []
    for live_status in LiveStatus.objects(team_code=code):
        player = Player.objects(device_id=live_status.device_id).first()
        if consent_blocked(player, team):
            continue

        updated_at = live_status.updated_at
        players.append({
            'player_name': player_display_name(player),
            'updated_at': _format_timestamp(updated_at),
            'x': live_status.x,
            'y': live_status.y,
            'heart_rate': live_status.heart_rate,
            'distance_m': live_status.distance_m,
            'on_pitch': bool(live_status.on_pitch),
            'stale': updated_at is None or updated_at < stale_cutoff,
            'elapsed_s': live_status.elapsed_s,
            'current_speed': live_status.current_speed,
            'us_goals': live_status.us_goals,
            'them_goals': live_status.them_goals,
        })

    return jsonify({'players': players})
