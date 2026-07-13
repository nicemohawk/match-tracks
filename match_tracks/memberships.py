"""Multi-team membership (V2 §6).

A device may belong to many teams via the ``device_teams`` collection;
``players.team_code`` remains the *default* team for V1 compatibility and
counts as an implicit membership.
"""

from flask import Blueprint, jsonify, request

from match_tracks.auth import auth, current_principal, effective_device_id
from match_tracks.models import DeviceTeamMembership, Player, Team

memberships_blueprint = Blueprint('memberships', __name__)

TIMESTAMP_FORMAT = '%Y-%m-%dT%H:%M:%SZ'


def is_member(device_id, team_code):
    """True when the device belongs to the team (membership row or legacy default)."""
    if not device_id or not team_code:
        return False
    device_id = device_id.lower()
    if DeviceTeamMembership.objects(device_id=device_id, team_code=team_code).first():
        return True
    player = Player.objects(device_id=device_id).first()
    return bool(player and player.team_code == team_code)


def require_member(team_code):
    """None when the caller may act on the team; else a (response, 403) tuple."""
    principal = current_principal()
    if principal and principal.get('admin'):
        return None
    if is_member(effective_device_id(), team_code):
        return None
    return jsonify({'reason': 'not_a_member'}), 403


def actor_denial(identifier):
    """None when the caller may act as device `identifier`; else (response, 403)."""
    principal = current_principal()
    if principal and principal.get('admin'):
        return None
    if effective_device_id() == str(identifier).lower():
        return None
    return jsonify({'reason': 'not_your_device'}), 403


# JOIN a team
@memberships_blueprint.route('/devices/<identifier>/teams', methods=['POST'])
@auth.login_required
def join_team(identifier):
    denial = actor_denial(identifier)
    if denial:
        return denial

    json_data = request.get_json(silent=True) or {}
    team_code = json_data.get('team_code')
    if not team_code:
        return jsonify({'reason': 'team_code_required'}), 400

    if Team.objects(code=team_code).first() is None:
        Team(code=team_code, name=None).save()

    device_id = str(identifier).lower()
    existing = DeviceTeamMembership.objects(device_id=device_id, team_code=team_code).first()
    if existing is not None:
        return jsonify({'joined': False, 'team_code': team_code}), 200

    DeviceTeamMembership(device_id=device_id, team_code=team_code).save()
    return jsonify({'joined': True, 'team_code': team_code}), 201


# LEAVE a team
@memberships_blueprint.route('/devices/<identifier>/teams/<code>', methods=['DELETE'])
@auth.login_required
def leave_team(identifier, code):
    denial = actor_denial(identifier)
    if denial:
        return denial

    DeviceTeamMembership.objects(device_id=str(identifier).lower(), team_code=code).delete()
    return '', 204


# LIST memberships
@memberships_blueprint.route('/devices/<identifier>/teams', methods=['GET'])
@auth.login_required
def list_teams(identifier):
    denial = actor_denial(identifier)
    if denial:
        return denial

    memberships = DeviceTeamMembership.objects(
        device_id=str(identifier).lower()).order_by('joined_at')
    teams = []
    for membership in memberships:
        team = Team.objects(code=membership.team_code).first()
        teams.append({
            'team_code': membership.team_code,
            'team_name': team.name if team else None,
            'joined_at': membership.joined_at.strftime(TIMESTAMP_FORMAT)
            if membership.joined_at else None,
        })
    return jsonify({'teams': teams})
