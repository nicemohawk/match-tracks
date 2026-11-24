"""Captain-led team endpoints (V3 §2–§7).

The team roster and its opaque per-member identity (§2), member management and
code rotation (§3), the captain/coach-set lineup and next match (§4), the
played-together match history (§5), the public invite page + AASA (§6), and the
minors join-request queue (§7). Reads require membership; writes require the
device secret and the right role. Every code in a URL is resolved through
``memberships.canonical_team_code`` so rotated codes keep working.
"""

import hashlib
import json
from datetime import datetime, timedelta

from flask import Blueprint, current_app, jsonify, request

from match_tracks import memberships
from match_tracks.auth import auth, effective_device_id, require_device_secret
from match_tracks.models import (DeviceTeamMembership, JoinRequest, Match,
                                 Player, Team, TeamLineup, TeamNext)
from match_tracks.privacy import consent_blocked, player_display_name
from match_tracks.rate_limit import rate_limited

teams_blueprint = Blueprint('teams', __name__)

TIMESTAMP_FORMAT = '%Y-%m-%dT%H:%M:%SZ'
NAME_MAX_LENGTH = 60
MAX_LINEUP_SLOTS = 11
NEXT_CLEAR_AFTER = timedelta(hours=12)
MATCH_GROUP_WINDOW = timedelta(hours=2)
DEFAULT_APPLE_APP_ID_PREFIX = 'TEAMID'
DEFAULT_APP_STORE_URL = 'https://apps.apple.com/app/matchtracker/id0000000000'


def _parse_timestamp(value):
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
    return value.strftime(TIMESTAMP_FORMAT) if value is not None else None


def _resolve_team_or_404(code):
    """(team, canonical_code) for a presented code, or (None, canonical)."""
    canonical = memberships.canonical_team_code(code) or code
    return Team.objects(code=canonical).first(), canonical


def _acting_secret_denial():
    """Require the acting device's secret for a §2–§6 write (admin bypasses)."""
    return require_device_secret(effective_device_id())


# --- §2 the team -----------------------------------------------------------

def _members_payload(team, canonical, viewer_device_id):
    """The members list for GET /teams/<code>, newest-sharer fields included.

    Removed members are excluded (they are not active rows); a consent-blocked
    member is hidden from everyone but themselves (§7).
    """
    memberships.ensure_team_roles(canonical)
    members = []
    for row in DeviceTeamMembership.objects(
            team_code=canonical, removed_at=None).order_by('joined_at'):
        player = Player.objects(device_id=row.device_id).first()
        if consent_blocked(player, team) and row.device_id != viewer_device_id:
            continue
        shared = Match.objects(device_id=row.device_id, team_code=canonical)
        matches_shared = shared.count()
        latest = shared.order_by('-recorded_at').first()
        members.append({
            'member_id': row.member_id,
            'player_name': player_display_name(player) or 'No name',
            'role': row.role,
            'joined_at': _format_timestamp(row.joined_at),
            'matches_shared': matches_shared,
            'last_shared_at': _format_timestamp(latest.recorded_at) if latest else None,
        })
    return members


def _etag_for(body):
    digest = hashlib.sha256(
        json.dumps(body, sort_keys=True, default=str).encode('utf-8')).hexdigest()
    return f'"{digest}"'


@teams_blueprint.route('/teams/<code>', methods=['GET'])
@auth.login_required
@rate_limited('read')
def get_team(code):
    team, canonical = _resolve_team_or_404(code)
    if team is None:
        return jsonify({'reason': 'unknown_team'}), 404
    denial = memberships.require_member(canonical)
    if denial:
        return denial

    viewer = effective_device_id()
    members = _members_payload(team, canonical, viewer)  # backfills member_ids
    my_membership = memberships.active_membership(viewer, canonical)
    body = {
        'code': memberships.advertised_code(team),
        'name': team.name,
        'created_at': _format_timestamp(team.created_at),
        'my_member_id': my_membership.member_id if my_membership else None,
        'members': members,
    }

    etag = _etag_for(body)
    if request.headers.get('If-None-Match') == etag:
        response = current_app.response_class(status=304)
        response.headers['ETag'] = etag
        return response

    response = jsonify(body)
    response.headers['ETag'] = etag
    return response


def _valid_team_name(name):
    return (isinstance(name, str) and 1 <= len(name) <= NAME_MAX_LENGTH
            and '\n' not in name and '\r' not in name)


@teams_blueprint.route('/teams/<code>', methods=['PUT'])
@auth.login_required
@rate_limited('write')
def put_team(code):
    team, canonical = _resolve_team_or_404(code)
    if team is None:
        return jsonify({'reason': 'unknown_team'}), 404

    denial = memberships.require_member(canonical)
    if denial:
        return denial
    secret_denial = _acting_secret_denial()
    if secret_denial:
        return secret_denial

    json_data = request.get_json(silent=True) or {}
    name = json_data.get('name')
    if not _valid_team_name(name):
        return jsonify({'reason': 'invalid_name'}), 400

    # Captain only, except the first member may name a still-unnamed team (so a
    # team started offline gets its name with the membership call).
    memberships.ensure_team_roles(canonical)
    is_captain = memberships.role_of(effective_device_id(), canonical) == 'captain'
    principal = memberships.current_principal()
    is_admin = bool(principal and principal.get('admin'))
    first_member_naming = team.name is None and memberships.active_membership(
        effective_device_id(), canonical) is not None
    if not (is_captain or is_admin or first_member_naming):
        return jsonify({'reason': 'not_captain'}), 403

    team.name = name
    team.save()
    return jsonify({'code': memberships.advertised_code(team), 'name': team.name}), 200


# --- §2 per-device typed name ----------------------------------------------

@teams_blueprint.route('/devices/<identifier>/player', methods=['PUT'])
@auth.login_required
@rate_limited('write')
def put_device_player(identifier):
    denial = memberships.actor_denial(identifier)
    if denial:
        return denial
    secret_denial = require_device_secret(identifier)
    if secret_denial:
        return secret_denial

    json_data = request.get_json(silent=True) or {}
    player_name = json_data.get('player_name')
    if not isinstance(player_name, str) or not player_name.strip():
        return jsonify({'reason': 'player_name_required'}), 400

    device_id = str(identifier).lower()
    player = Player.objects(device_id=device_id).first() or Player(device_id=device_id)
    player.name = player_name  # stored exactly as typed
    player.updated_at = datetime.utcnow()
    player.save()
    return jsonify({'player_name': player.name}), 200


# --- §3 members ------------------------------------------------------------

@teams_blueprint.route('/teams/<code>/members/<member_id>', methods=['DELETE'])
@auth.login_required
@rate_limited('write')
def remove_member(code, member_id):
    team, canonical = _resolve_team_or_404(code)
    if team is None:
        return jsonify({'reason': 'unknown_team'}), 404
    captain_denial = memberships.require_captain(canonical)
    if captain_denial:
        return captain_denial
    secret_denial = _acting_secret_denial()
    if secret_denial:
        return secret_denial

    row = memberships.membership_by_member_id(canonical, member_id)
    if row is None:
        return jsonify({'reason': 'unknown_member'}), 404
    if row.removed_at is None:
        row.removed_at = datetime.utcnow()
        row.save()
        memberships.log_audit(canonical, 'remove', subject_member_id=member_id)
        memberships.reassign_captain(canonical)  # in case the captain removed self
    return '', 204


@teams_blueprint.route('/teams/<code>/members/<member_id>/restore', methods=['POST'])
@auth.login_required
@rate_limited('write')
def restore_member(code, member_id):
    team, canonical = _resolve_team_or_404(code)
    if team is None:
        return jsonify({'reason': 'unknown_team'}), 404
    captain_denial = memberships.require_captain(canonical)
    if captain_denial:
        return captain_denial
    secret_denial = _acting_secret_denial()
    if secret_denial:
        return secret_denial

    row = memberships.membership_by_member_id(canonical, member_id)
    if row is None:
        return jsonify({'reason': 'unknown_member'}), 404
    if row.removed_at is not None:
        row.removed_at = None
        if row.role == 'captain':  # never resurrect a second captain
            row.role = 'player'
        row.save()
        memberships.log_audit(canonical, 'restore', subject_member_id=member_id)
    return '', 204


@teams_blueprint.route('/teams/<code>/captain', methods=['POST'])
@auth.login_required
@rate_limited('write')
def hand_over_captain(code):
    team, canonical = _resolve_team_or_404(code)
    if team is None:
        return jsonify({'reason': 'unknown_team'}), 404
    captain_denial = memberships.require_captain(canonical)
    if captain_denial:
        return captain_denial
    secret_denial = _acting_secret_denial()
    if secret_denial:
        return secret_denial

    json_data = request.get_json(silent=True) or {}
    new_captain = memberships.membership_by_member_id(canonical, json_data.get('member_id'))
    if new_captain is None or new_captain.removed_at is not None:
        return jsonify({'reason': 'unknown_member'}), 404

    for row in DeviceTeamMembership.objects(
            team_code=canonical, role='captain', removed_at=None):
        if row.member_id != new_captain.member_id:
            row.role = 'player'
            row.save()
    new_captain.role = 'captain'
    new_captain.save()
    memberships.log_audit(canonical, 'handover', subject_member_id=new_captain.member_id)
    return '', 204


@teams_blueprint.route('/teams/<code>/members/<member_id>/role', methods=['PUT'])
@auth.login_required
@rate_limited('write')
def set_member_role(code, member_id):
    team, canonical = _resolve_team_or_404(code)
    if team is None:
        return jsonify({'reason': 'unknown_team'}), 404
    denial = memberships.require_member(canonical)
    if denial:
        return denial
    secret_denial = _acting_secret_denial()
    if secret_denial:
        return secret_denial

    json_data = request.get_json(silent=True) or {}
    new_role = json_data.get('role')
    if new_role not in ('coach', 'player'):
        # 'captain' is set only via the handover endpoint (V3 §3).
        return jsonify({'reason': 'use_handover' if new_role == 'captain'
                        else 'invalid_role'}), 400

    row = memberships.membership_by_member_id(canonical, member_id)
    if row is None or row.removed_at is not None:
        return jsonify({'reason': 'unknown_member'}), 404

    # Captain may set anyone; anyone may set their own role.
    acting = effective_device_id()
    principal = memberships.current_principal()
    is_admin = bool(principal and principal.get('admin'))
    acting_is_captain = memberships.role_of(acting, canonical) == 'captain'
    acting_self = acting is not None and acting == row.device_id
    if not (is_admin or acting_is_captain or acting_self):
        return jsonify({'reason': 'not_allowed'}), 403

    if row.role == 'captain':
        return jsonify({'reason': 'use_handover'}), 400

    row.role = new_role
    row.save()
    memberships.log_audit(canonical, 'role', subject_member_id=member_id, detail=new_role)
    return '', 204


@teams_blueprint.route('/teams/<code>/code', methods=['POST'])
@auth.login_required
@rate_limited('write')
def rotate_code(code):
    team, canonical = _resolve_team_or_404(code)
    if team is None:
        return jsonify({'reason': 'unknown_team'}), 404
    captain_denial = memberships.require_captain(canonical)
    if captain_denial:
        return captain_denial
    secret_denial = _acting_secret_denial()
    if secret_denial:
        return secret_denial

    new_code = _generate_unique_code()
    old_advertised = memberships.advertised_code(team)
    team.current_code = new_code
    team.previous_code = old_advertised
    team.previous_code_access_until = datetime.utcnow() + timedelta(days=30)
    team.save()
    return jsonify({'code': new_code}), 200


def _generate_unique_code():
    import secrets as secrets_module
    import string
    alphabet = string.ascii_uppercase + string.digits
    for _ in range(20):
        candidate = ''.join(secrets_module.choice(alphabet) for _ in range(6))
        if memberships.canonical_team_code(candidate) is None and \
                Team.objects(code=candidate).first() is None:
            return candidate
    # Fall back to a longer code on the vanishingly unlikely repeated collision.
    return ''.join(secrets_module.choice(alphabet) for _ in range(10))


# --- §4 lineup & next ------------------------------------------------------

def _monotonic_updated_at(previous):
    """Strictly-increasing (second-resolution) timestamp for optimistic locking."""
    now = datetime.utcnow()
    if previous is not None and now <= previous:
        return previous + timedelta(seconds=1)
    return now


def _if_match_conflict(existing_updated_at):
    """True when an If-Match header disagrees with the stored version."""
    if_match = request.headers.get('If-Match')
    if if_match is None or existing_updated_at is None:
        return False
    return if_match.strip('"') != _format_timestamp(existing_updated_at)


@teams_blueprint.route('/teams/<code>/lineup', methods=['GET'])
@auth.login_required
@rate_limited('read')
def get_lineup(code):
    team, canonical = _resolve_team_or_404(code)
    if team is None:
        return jsonify({'reason': 'unknown_team'}), 404
    denial = memberships.require_member(canonical)
    if denial:
        return denial

    lineup = TeamLineup.objects(team_code=canonical).first()
    if lineup is None:
        return jsonify({'reason': 'no_lineup'}), 404

    updater = Player.objects(device_id=lineup.updated_by_device).first() \
        if lineup.updated_by_device else None
    return jsonify({
        'updated_at': _format_timestamp(lineup.updated_at),
        'updated_by': player_display_name(updater),  # initials-only honored (§7)
        'name': lineup.name,
        'slots': lineup.slots or [],
    })


def _clean_slots(raw_slots):
    """Validate and normalize lineup slots, or raise ValueError.

    Each slot has exactly one of member_id / name and x,y in [0,1]; at most 11.
    A ``name`` slot is kept verbatim (an off-app person) and nothing more.
    """
    if not isinstance(raw_slots, list) or len(raw_slots) > MAX_LINEUP_SLOTS:
        raise ValueError('slots')
    cleaned = []
    for slot in raw_slots:
        if not isinstance(slot, dict):
            raise ValueError('slot')
        member_id = slot.get('member_id')
        name = slot.get('name')
        if bool(member_id) == bool(name):  # exactly one of the two
            raise ValueError('identity')
        x, y = slot.get('x'), slot.get('y')
        if not (_is_unit(x) and _is_unit(y)):
            raise ValueError('coords')
        cleaned.append({'member_id': member_id, 'name': name,
                        'x': float(x), 'y': float(y)})
    return cleaned


def _is_unit(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and 0.0 <= float(value) <= 1.0


@teams_blueprint.route('/teams/<code>/lineup', methods=['PUT'])
@auth.login_required
@rate_limited('write')
def put_lineup(code):
    team, canonical = _resolve_team_or_404(code)
    if team is None:
        return jsonify({'reason': 'unknown_team'}), 404
    role_denial = memberships.require_coach_or_captain(canonical)
    if role_denial:
        return role_denial
    secret_denial = _acting_secret_denial()
    if secret_denial:
        return secret_denial

    json_data = request.get_json(silent=True) or {}
    try:
        slots = _clean_slots(json_data.get('slots', []))
    except ValueError:
        return jsonify({'reason': 'invalid_lineup'}), 400
    name = json_data.get('name')
    if name is not None and not isinstance(name, str):
        return jsonify({'reason': 'invalid_lineup'}), 400

    lineup = TeamLineup.objects(team_code=canonical).first()
    if lineup is not None and _if_match_conflict(lineup.updated_at):
        return jsonify({'reason': 'stale'}), 412

    previous_updated_at = lineup.updated_at if lineup is not None else None
    lineup = lineup or TeamLineup(team_code=canonical)
    lineup.name = name
    lineup.slots = slots
    lineup.updated_by_device = effective_device_id()
    lineup.updated_at = _monotonic_updated_at(previous_updated_at)
    lineup.save()
    return '', 204


@teams_blueprint.route('/teams/<code>/next', methods=['GET'])
@auth.login_required
@rate_limited('read')
def get_next(code):
    team, canonical = _resolve_team_or_404(code)
    if team is None:
        return jsonify({'reason': 'unknown_team'}), 404
    denial = memberships.require_member(canonical)
    if denial:
        return denial

    nxt = TeamNext.objects(team_code=canonical).first()
    if nxt is None or _next_expired(nxt):
        return jsonify({'reason': 'no_next'}), 404

    return jsonify({
        'kickoff': _format_timestamp(nxt.kickoff),
        'title': nxt.title,
        'place': nxt.place,
        'in': nxt.entries or [],
        'updated_at': _format_timestamp(nxt.updated_at),
    })


def _next_expired(nxt):
    return (nxt.kickoff is not None
            and datetime.utcnow() > nxt.kickoff + NEXT_CLEAR_AFTER)


def _clean_next_entries(raw_entries):
    if not isinstance(raw_entries, list):
        raise ValueError('in')
    cleaned = []
    for entry in raw_entries:
        if not isinstance(entry, dict):
            raise ValueError('entry')
        member_id = entry.get('member_id')
        name = entry.get('name')
        if bool(member_id) == bool(name):
            raise ValueError('identity')
        cleaned.append({'member_id': member_id} if member_id else {'name': name})
    return cleaned


@teams_blueprint.route('/teams/<code>/next', methods=['PUT'])
@auth.login_required
@rate_limited('write')
def put_next(code):
    team, canonical = _resolve_team_or_404(code)
    if team is None:
        return jsonify({'reason': 'unknown_team'}), 404
    role_denial = memberships.require_coach_or_captain(canonical)
    if role_denial:
        return role_denial
    secret_denial = _acting_secret_denial()
    if secret_denial:
        return secret_denial

    json_data = request.get_json(silent=True) or {}
    try:
        entries = _clean_next_entries(json_data.get('in', []))
    except ValueError:
        return jsonify({'reason': 'invalid_next'}), 400

    nxt = TeamNext.objects(team_code=canonical).first()
    if nxt is not None and not _next_expired(nxt) and _if_match_conflict(nxt.updated_at):
        return jsonify({'reason': 'stale'}), 412

    previous_updated_at = nxt.updated_at if nxt is not None else None
    nxt = nxt or TeamNext(team_code=canonical)
    nxt.kickoff = _parse_timestamp(json_data.get('kickoff'))
    nxt.title = json_data.get('title')
    nxt.place = json_data.get('place')  # free text; no coordinates
    nxt.entries = entries
    nxt.updated_at = _monotonic_updated_at(previous_updated_at)
    nxt.save()
    return '', 204


# --- §5 matches played together --------------------------------------------

def _match_player_row(match, member_id, player_name):
    stats = match.stats or {}
    time_on_pitch = stats.get('time_on_pitch_s')
    distance = stats.get('total_distance_m')
    sprints = stats.get('sprint_count')
    events = match.events or []
    goals = assists = None
    if events:
        goals = sum(1 for event in events if event.get('kind') in ('goalFor', 'goalMine'))
        assists = sum(1 for event in events if event.get('kind') == 'assist')
    return {
        'member_id': member_id,
        'player_name': player_name,
        'minutes': round(time_on_pitch / 60.0) if time_on_pitch is not None else None,
        'distance_m': distance if distance is not None else None,
        'goals': goals,
        'assists': assists,
        'sprints': sprints if sprints is not None else None,
    }


@teams_blueprint.route('/teams/<code>/matches', methods=['GET'])
@auth.login_required
@rate_limited('read')
def get_team_matches(code):
    team, canonical = _resolve_team_or_404(code)
    if team is None:
        return jsonify({'reason': 'unknown_team'}), 404
    denial = memberships.require_member(canonical)
    if denial:
        return denial

    memberships.ensure_team_roles(canonical)
    member_id_by_device = {}
    for row in DeviceTeamMembership.objects(team_code=canonical, removed_at=None):
        member_id_by_device[row.device_id] = row.member_id
    removed_devices = {
        row.device_id
        for row in DeviceTeamMembership.objects(team_code=canonical, removed_at__ne=None)}

    since = _parse_timestamp(request.args.get('since'))
    limit = request.args.get('limit', default=20, type=int)
    if limit > 100:
        limit = 100
    if limit < 0:
        limit = 0

    query = Match.objects(team_code=canonical).order_by('recorded_at')
    if since is not None:
        query = query.filter(recorded_at__gte=since)

    # Group uploads whose kickoffs fall within two hours (the TeamMinutes rule).
    groups = []
    current = None
    for match in query:
        if match.recorded_at is None or match.device_id in removed_devices:
            continue
        player = Player.objects(device_id=match.device_id).first()
        if consent_blocked(player, team):
            continue
        if current is None or match.recorded_at - current['latest'] > MATCH_GROUP_WINDOW:
            current = {'kickoff': match.recorded_at, 'latest': match.recorded_at,
                       'length_minutes': None, 'matches': []}
            groups.append(current)
        current['latest'] = match.recorded_at
        if match.duration_s is not None:
            minutes = round(match.duration_s / 60.0)
            current['length_minutes'] = (minutes if current['length_minutes'] is None
                                         else max(current['length_minutes'], minutes))
        current['matches'].append((match, player))

    result = []
    for group in reversed(groups):  # newest first
        players = []
        for match, player in group['matches']:
            players.append(_match_player_row(
                match, member_id_by_device.get(match.device_id),
                player_display_name(player) or 'No name'))
        result.append({
            'kickoff': _format_timestamp(group['kickoff']),
            'length_minutes': group['length_minutes'],
            'players': players,
        })

    return jsonify({'matches': result[:limit]})


# --- §6 invites ------------------------------------------------------------

@teams_blueprint.route('/join/<code>', methods=['GET'])
def join_page(code):
    """A shareable invite page. No member names and no confirmation that a code
    exists — the team name comes from the link's own ``?team=`` query (§6)."""
    team_name = request.args.get('team', '').strip()
    app_store_url = current_app.config.get('APP_STORE_URL', DEFAULT_APP_STORE_URL)
    safe_code = _html_escape(code)
    safe_team = _html_escape(team_name) if team_name else 'a MatchTracker team'
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Join {safe_team} on MatchTracker</title></head>
<body style="font-family:-apple-system,system-ui,sans-serif;max-width:32rem;margin:3rem auto;padding:0 1rem;text-align:center">
<h1>Join {safe_team}</h1>
<p>Open MatchTracker and enter this code:</p>
<p style="font-size:2rem;font-weight:700;letter-spacing:0.2em">{safe_code}</p>
<p><a href="{_html_escape(app_store_url)}">Get MatchTracker on the App Store</a></p>
</body></html>"""
    return current_app.response_class(html, mimetype='text/html')


def _html_escape(value):
    return (str(value).replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;').replace("'", '&#39;'))


@teams_blueprint.route('/.well-known/apple-app-site-association', methods=['GET'])
def apple_app_site_association():
    prefix = current_app.config.get('APPLE_APP_ID_PREFIX', DEFAULT_APPLE_APP_ID_PREFIX)
    body = {'applinks': {'details': [{
        'appIDs': [f'{prefix}.com.nicemohawk.MatchTracker'],
        'components': [{'/': '/join/*'}],
    }]}}
    return current_app.response_class(
        json.dumps(body), mimetype='application/json')


# --- §7 minors join requests -----------------------------------------------

@teams_blueprint.route('/teams/<code>/requests', methods=['GET'])
@auth.login_required
@rate_limited('read')
def list_join_requests(code):
    team, canonical = _resolve_team_or_404(code)
    if team is None:
        return jsonify({'reason': 'unknown_team'}), 404
    captain_denial = memberships.require_captain(canonical)
    if captain_denial:
        return captain_denial

    requests = []
    for req in JoinRequest.objects(team_code=canonical).order_by('requested_at'):
        player = Player.objects(device_id=req.device_id).first()
        requests.append({
            'member_id': req.member_id,
            'player_name': player_display_name(player) or 'No name',
            'requested_at': _format_timestamp(req.requested_at),
        })
    return jsonify({'requests': requests})


@teams_blueprint.route('/teams/<code>/requests/<member_id>', methods=['POST'])
@auth.login_required
@rate_limited('write')
def decide_join_request(code, member_id):
    team, canonical = _resolve_team_or_404(code)
    if team is None:
        return jsonify({'reason': 'unknown_team'}), 404
    captain_denial = memberships.require_captain(canonical)
    if captain_denial:
        return captain_denial
    secret_denial = _acting_secret_denial()
    if secret_denial:
        return secret_denial

    req = JoinRequest.objects(team_code=canonical, member_id=member_id).first()
    if req is None:
        return jsonify({'reason': 'unknown_request'}), 404

    accept = bool((request.get_json(silent=True) or {}).get('accept'))
    if accept:
        from mongoengine import NotUniqueError
        if memberships.active_membership(req.device_id, canonical) is None:
            try:
                DeviceTeamMembership(device_id=req.device_id, team_code=canonical,
                                     member_id=req.member_id, role='player').save()
            except NotUniqueError:
                pass
        memberships.ensure_team_roles(canonical)
    req.delete()
    return '', 204
