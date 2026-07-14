"""Team lifecycle management (proactive team admin).

Read/rename/archive a team and claim ownership of an auto-created stub. See
docs/team-management-contract.md for the frozen contract. Ownership is
creator-owned: the first device to create/claim a team owns it, and only the
owner (or an admin key) may rename, archive, or toggle its consent gate.
Deletion is a reversible soft-archive that preserves all matches and stats.
"""

from datetime import datetime

from flask import Blueprint, jsonify, request

from match_tracks.auth import (auth, current_principal, effective_device_id)
from match_tracks.memberships import (owner_denial, principal_owns_team,
                                     require_member)
from match_tracks.models import DeviceTeamMembership, Player, Team

team_admin_blueprint = Blueprint('team_admin', __name__)

TIMESTAMP_FORMAT = '%Y-%m-%dT%H:%M:%SZ'


def _member_count(team_code):
    """Distinct devices across explicit memberships and legacy default rosters."""
    device_ids = set(
        DeviceTeamMembership.objects(team_code=team_code).distinct('device_id'))
    device_ids.update(Player.objects(team_code=team_code).distinct('device_id'))
    return len(device_ids)


def _team_detail_json(team):
    """Team view returned by GET/PATCH — never exposes the raw owner id."""
    return {
        'code': team.code,
        'name': team.name,
        'requires_consent': bool(team.requires_consent),
        'archived': bool(team.archived),
        'created_at': team.created_at.strftime(TIMESTAMP_FORMAT) if team.created_at else None,
        'member_count': _member_count(team.code),
        'is_owner': principal_owns_team(team),
    }


# READ a team (any member; admin bypass)
@team_admin_blueprint.route('/teams/<code>', methods=['GET'])
@auth.login_required
def get_team(code):
    team = Team.objects(code=code).first()
    if team is None:
        return jsonify({'reason': 'unknown_team'}), 404
    denial = require_member(code)
    if denial:
        return denial
    return jsonify({'team': _team_detail_json(team)})


# RENAME / toggle consent / (un)archive a team (owner or admin)
@team_admin_blueprint.route('/teams/<code>', methods=['PATCH'])
@auth.login_required
def patch_team(code):
    team = Team.objects(code=code).first()
    if team is None:
        return jsonify({'reason': 'unknown_team'}), 404
    denial = owner_denial(team)
    if denial:
        return denial

    json_data = request.get_json(silent=True) or {}
    if 'name' in json_data:
        team.name = json_data.get('name')
    if 'requires_consent' in json_data:
        team.requires_consent = bool(json_data.get('requires_consent'))
    if 'archived' in json_data:
        archived = bool(json_data.get('archived'))
        team.archived = archived
        team.archived_at = datetime.utcnow() if archived else None
    team.save()
    return jsonify({'team': _team_detail_json(team)}), 200


# DELETE a team → reversible soft-archive (owner or admin)
@team_admin_blueprint.route('/teams/<code>', methods=['DELETE'])
@auth.login_required
def delete_team(code):
    team = Team.objects(code=code).first()
    if team is None:
        return jsonify({'reason': 'unknown_team'}), 404
    denial = owner_denial(team)
    if denial:
        return denial

    # Idempotent: archiving an already-archived team still returns 200. Matches,
    # memberships, and stats are all preserved.
    team.archived = True
    team.archived_at = datetime.utcnow()
    team.save()
    return jsonify({'code': code, 'archived': True}), 200


# CLAIM ownership of an (unowned) team (any member; admin may transfer)
@team_admin_blueprint.route('/teams/<code>/claim', methods=['POST'])
@auth.login_required
def claim_team(code):
    team = Team.objects(code=code).first()
    if team is None:
        return jsonify({'reason': 'unknown_team'}), 404
    denial = require_member(code)
    if denial:
        return denial

    principal = current_principal()
    is_admin = bool(principal and principal.get('admin'))
    actor = effective_device_id()

    # Already the owner → idempotent no-op.
    if team.owner_device_id is not None and team.owner_device_id == actor:
        return jsonify({'claimed': False, 'is_owner': True}), 200

    # Owned by another device: only an admin (with an X-Device-ID) may transfer.
    if team.owner_device_id is not None and not is_admin:
        return jsonify({'reason': 'already_owned'}), 403

    # Unowned, or an admin transferring: bind ownership to the acting device.
    if actor is None:
        return jsonify({'reason': 'device_required'}), 400
    team.owner_device_id = actor
    team.save()
    return jsonify({'claimed': True, 'is_owner': True}), 200
