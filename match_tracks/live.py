"""Live match telemetry (V2 §1).

Each device posts a running snapshot of its live match to
``POST /devices/<device_id>/live``; the server keeps exactly one row per
device (``LiveStatus``, primary key ``device_id``) and overwrites it on every
accepted update. Teammates poll ``GET /teams/<code>/live`` for the roster's
latest positions. Updates are sequence-guarded so an out-of-order or replayed
snapshot never clobbers a newer one.
"""

from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request

from match_tracks import memberships
from match_tracks.auth import auth
from match_tracks.models import DeviceTeamMembership, LiveStatus, Player, Team
from match_tracks.privacy import consent_blocked, player_display_name
from match_tracks.rate_limit import rate_limited

live_blueprint = Blueprint('live', __name__)

TIMESTAMP_FORMAT = '%Y-%m-%dT%H:%M:%SZ'
STALE_AFTER_SECONDS = 30


def _parse_timestamp(value):
    """Parse an incoming ISO-8601 timestamp, tolerating a trailing 'Z'.

    Python 3.9's ``datetime.fromisoformat`` rejects the trailing 'Z', so strip
    it first. Returns ``None`` for empty or unparseable input.
    """
    if not value:
        return None
    text = str(value).strip()
    if text.endswith('Z'):
        text = text[:-1]
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _format_timestamp(value):
    """Serialize a datetime as ``...Z`` (UTC, second precision), or ``None``."""
    if value is None:
        return None
    return value.strftime(TIMESTAMP_FORMAT)


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

    V3 §0: live is free — no team entitlement is required to post or read it.
    """
    denial = memberships.actor_denial(device_id)
    if denial:
        return denial

    json_data = request.get_json(silent=True) or {}
    team_code = memberships.canonical_team_code(json_data.get('team_code')) \
        or json_data.get('team_code')

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

    updated_at = _parse_timestamp(json_data.get('timestamp')) or datetime.utcnow()

    live_status = LiveStatus(device_id=normalized_device_id)
    live_status.team_code = team_code
    live_status.match_uuid = normalized_match_uuid
    live_status.sequence = incoming_sequence
    live_status.updated_at = updated_at
    live_status.elapsed_s = json_data.get('elapsed_s')
    # V3 §0: heart_rate is dropped on ingest and never returned. Older builds
    # may still send it; it is ignored.
    live_status.distance_m = json_data.get('distance_m')
    live_status.current_speed = json_data.get('current_speed')
    live_status.on_pitch = bool(json_data.get('on_pitch', True))
    live_status.us_goals = json_data.get('us_goals')
    live_status.them_goals = json_data.get('them_goals')
    live_status.x = json_data.get('x')
    live_status.y = json_data.get('y')
    live_status.save()

    # §11: append any live-tagged events to the team event log (provisional),
    # only now that the sequence guard has passed (a stale POST never appends).
    from match_tracks.events import ingest_live_events
    ingest_live_events(team_code, normalized_match_uuid, normalized_device_id,
                       json_data.get('new_events'))

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
    canonical = memberships.canonical_team_code(code) or code
    member_denial = memberships.require_member(canonical)
    if member_denial:
        return member_denial

    team = Team.objects(code=canonical).first()
    memberships.ensure_team_roles(canonical)
    member_ids = {
        row.device_id: row.member_id
        for row in DeviceTeamMembership.objects(team_code=canonical, removed_at=None)}
    removed_devices = {
        row.device_id
        for row in DeviceTeamMembership.objects(team_code=canonical, removed_at__ne=None)}

    now = datetime.utcnow()
    stale_cutoff = now - timedelta(seconds=STALE_AFTER_SECONDS)
    # V3 §7: requires_consent teams never leak positions.
    omit_coordinates = bool(team is not None and team.requires_consent)

    players = []
    for live_status in LiveStatus.objects(team_code=canonical):
        if live_status.device_id in removed_devices:
            continue  # a removed member's live data leaves the team view
        player = Player.objects(device_id=live_status.device_id).first()
        if consent_blocked(player, team):
            continue

        updated_at = live_status.updated_at
        # No device id, heart rate, route or effort ever leaves here (V3 §7).
        players.append({
            'member_id': member_ids.get(live_status.device_id),
            'player_name': player_display_name(player) or 'No name',
            'updated_at': _format_timestamp(updated_at),
            'x': None if omit_coordinates else live_status.x,
            'y': None if omit_coordinates else live_status.y,
            'distance_m': live_status.distance_m,
            'on_pitch': bool(live_status.on_pitch),
            'stale': updated_at is None or updated_at < stale_cutoff,
            'elapsed_s': live_status.elapsed_s,
            'current_speed': live_status.current_speed,
            'us_goals': live_status.us_goals,
            'them_goals': live_status.them_goals,
        })

    return jsonify({'players': players})
