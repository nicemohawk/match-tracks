"""Multi-team membership (V2 §6) + captain-led roles (V3 §1/§3).

A device may belong to many teams via the ``device_teams`` collection;
``players.team_code`` remains the *default* team for V1 compatibility and
counts as an implicit membership. V3 adds per-team ``member_id`` (the only
identity exposed about a teammate — never a device id), a ``role``
(captain|coach|player), soft removal (``removed_at``), a rotatable join code,
and a per-device secret.
"""

import secrets
import uuid as uuid_module
from datetime import datetime

from flask import Blueprint, jsonify, request
from mongoengine import NotUniqueError

from match_tracks import join_rate
from match_tracks.auth import (auth, current_principal, effective_device_id,
                              hash_device_secret)
from match_tracks.models import (AuditLog, DeviceSecret, DeviceTeamMembership,
                                 JoinRequest, Player, Team)

memberships_blueprint = Blueprint('memberships', __name__)

TIMESTAMP_FORMAT = '%Y-%m-%dT%H:%M:%SZ'


# --- team code resolution (V3 §3 rotatable codes) --------------------------

def canonical_team_code(presented_code):
    """Map a presented join code to its team's immutable ``code``, or None.

    Resolves the live ``current_code`` and, within the 30-day grace, a rotated
    team's ``previous_code``. Pre-V3 teams (no ``current_code``) resolve by their
    original code. Returns None for unknown or expired codes.
    """
    if not presented_code:
        return None
    team = Team.objects(code=presented_code).first()
    if team is not None:
        if team.current_code and team.current_code != team.code:
            # Rotated away from this original code: valid only during grace.
            if team.previous_code == presented_code and _grace_active(team):
                return team.code
            return None
        return team.code
    team = Team.objects(current_code=presented_code).first()
    if team is not None:
        return team.code
    team = Team.objects(previous_code=presented_code).first()
    if team is not None and _grace_active(team):
        return team.code
    return None


def _grace_active(team):
    return (team.previous_code_access_until is not None
            and team.previous_code_access_until > datetime.utcnow())


def resolve_join(presented_code):
    """Classify a join code: ('ok', team) | ('rotated', team) | ('create', None).

    'rotated' means an old code whose team has moved on — joins are refused at
    once (V3 §3), independent of the read grace.
    """
    team = Team.objects(current_code=presented_code).first()
    if team is not None:
        return ('ok', team)
    team = Team.objects(code=presented_code).first()
    if team is not None:
        if not team.current_code or team.current_code == team.code:
            return ('ok', team)
        return ('rotated', team)
    team = Team.objects(previous_code=presented_code).first()
    if team is not None:
        return ('rotated', team)
    return ('create', None)


def advertised_code(team):
    """The code the app should show/send for a team (its current join code)."""
    return team.current_code or team.code


# --- membership + role helpers ---------------------------------------------

def is_member(device_id, team_code):
    """True when the device belongs to the team (active row or legacy default).

    A soft-removed membership (``removed_at`` set) does NOT count as a member.
    """
    if not device_id or not team_code:
        return False
    device_id = device_id.lower()
    if DeviceTeamMembership.objects(device_id=device_id, team_code=team_code,
                                    removed_at=None).first():
        return True
    player = Player.objects(device_id=device_id).first()
    return bool(player and player.team_code == team_code)


def active_membership(device_id, team_code):
    """The caller's non-removed membership row for the team, or None."""
    if not device_id:
        return None
    return DeviceTeamMembership.objects(
        device_id=device_id.lower(), team_code=team_code, removed_at=None).first()


def removed_membership(device_id, team_code):
    """The caller's soft-removed membership row for the team, or None."""
    if not device_id:
        return None
    return DeviceTeamMembership.objects(
        device_id=device_id.lower(), team_code=team_code,
        removed_at__ne=None).first()


def membership_by_member_id(team_code, member_id):
    """A team's membership row by its opaque member_id (removed or not)."""
    if not member_id:
        return None
    return DeviceTeamMembership.objects(team_code=team_code, member_id=member_id).first()


def role_of(device_id, team_code):
    """'captain'|'coach'|'player' for an active member, else None."""
    membership = active_membership(device_id, team_code)
    return membership.role if membership else None


def ensure_team_roles(team_code):
    """Backfill member_id and guarantee exactly one captain among active members.

    Idempotent and lazy (V3 §1): keeps live data correct whether or not the
    batch migration has run. The earliest member (by ``joined_at``) is captain
    when none is set; extra captains are demoted to the earliest.
    """
    active = list(DeviceTeamMembership.objects(
        team_code=team_code, removed_at=None).order_by('joined_at'))
    if not active:
        return
    for membership in active:
        if not membership.member_id:
            membership.member_id = str(uuid_module.uuid4())
            try:
                membership.save()
            except NotUniqueError:  # astronomically unlikely uuid clash
                membership.member_id = str(uuid_module.uuid4())
                membership.save()
    captains = [membership for membership in active if membership.role == 'captain']
    if not captains:
        active[0].role = 'captain'
        active[0].save()
    elif len(captains) > 1:
        keep = min(captains, key=lambda m: (m.joined_at or datetime.min))
        for membership in captains:
            if membership.id != keep.id:
                membership.role = 'player'
                membership.save()


def reassign_captain(team_code):
    """Promote the longest-standing active member to captain if none remains."""
    ensure_team_roles(team_code)


# --- authorization gates (all bypass admin, like the rest of the codebase) --

def require_member(team_code):
    """None when the caller may read/act on the team; else (response, 403).

    A soft-removed device gets ``removed``; an unknown device-less caller (old
    build with no X-Device-ID) is allowed — membership unknown (V3 §0).
    """
    principal = current_principal()
    if principal and principal.get('admin'):
        return None
    device_id = effective_device_id()
    if device_id is None:
        return None  # membership unknown → allow the read (V3 §0)
    if removed_membership(device_id, team_code):
        return jsonify({'reason': 'removed'}), 403
    if is_member(device_id, team_code):
        return None
    return jsonify({'reason': 'not_a_member'}), 403


def require_captain(team_code):
    """None when the caller is the captain (or admin); else (response, 403)."""
    principal = current_principal()
    if principal and principal.get('admin'):
        return None
    if role_of(effective_device_id(), team_code) == 'captain':
        return None
    return jsonify({'reason': 'not_captain'}), 403


def require_coach_or_captain(team_code):
    """None when the caller may set formation/who's-in; else (response, 403)."""
    principal = current_principal()
    if principal and principal.get('admin'):
        return None
    if role_of(effective_device_id(), team_code) in ('coach', 'captain'):
        return None
    return jsonify({'reason': 'not_allowed'}), 403


def actor_denial(identifier):
    """None when the caller may act as device `identifier`; else (response, 403)."""
    principal = current_principal()
    if principal and principal.get('admin'):
        return None
    if effective_device_id() == str(identifier).lower():
        return None
    return jsonify({'reason': 'not_your_device'}), 403


def principal_owns_team(team):
    """True when the caller may manage `team` (admin, or the owning device).

    Admin keys manage any team. A device owns a team only when the team has a
    resolved owner that matches the caller's effective device id.
    """
    principal = current_principal()
    if principal and principal.get('admin'):
        return True
    return (team.owner_device_id is not None
            and effective_device_id() == team.owner_device_id)


def owner_denial(team):
    """None when the caller owns `team`; else a (response, 403) tuple.

    A missing team is the caller's responsibility (404) before this is called.
    """
    if principal_owns_team(team):
        return None
    return jsonify({'reason': 'not_team_owner'}), 403


def log_audit(team_code, action, subject_member_id=None, detail=None):
    """Record a role change / removal for 90 days (V3 §3/§7)."""
    AuditLog(team_code=team_code, action=action,
             actor_device=effective_device_id(),
             subject_member_id=subject_member_id, detail=detail).save()


# JOIN a team
@memberships_blueprint.route('/devices/<identifier>/teams', methods=['POST'])
@auth.login_required
def join_team(identifier):
    denial = actor_denial(identifier)
    if denial:
        return denial

    device_id = str(identifier).lower()
    if not join_rate.join_allowed(device_id, request.remote_addr):
        return jsonify({'reason': 'rate_limited'}), 429

    json_data = request.get_json(silent=True) or {}
    presented_code = json_data.get('team_code')
    if not presented_code:
        return jsonify({'reason': 'team_code_required'}), 400

    status, team = resolve_join(presented_code)
    if status == 'rotated':
        return jsonify({'reason': 'code_rotated'}), 409
    if status == 'create':
        # Auto-create an unowned, unarchived stub (offline-first: no join friction).
        team = Team(code=presented_code, current_code=presented_code, name=None)
        team.save()
        team = Team.objects(code=presented_code).first()
    elif team.archived:
        return jsonify({'reason': 'team_archived'}), 409

    team_code = team.code  # canonical internal key

    if removed_membership(device_id, team_code):
        return jsonify({'reason': 'removed'}), 403

    existing = active_membership(device_id, team_code)
    if existing is not None:
        return jsonify({'joined': False, 'team_code': presented_code}), 200

    # A requires_consent team holds new joins as pending requests until the
    # captain accepts — except the very first member, who becomes captain so
    # someone can accept the rest (V3 §7).
    has_active_members = DeviceTeamMembership.objects(
        team_code=team_code, removed_at=None).first() is not None
    if team.requires_consent and has_active_members:
        existing_request = JoinRequest.objects(
            device_id=device_id, team_code=team_code).first()
        if existing_request is None:
            try:
                JoinRequest(member_id=str(uuid_module.uuid4()),
                            team_code=team_code, device_id=device_id).save()
            except NotUniqueError:
                pass
        return jsonify({'pending': True, 'team_code': presented_code}), 202

    role = 'player' if has_active_members else 'captain'
    try:
        DeviceTeamMembership(device_id=device_id, team_code=team_code,
                             member_id=str(uuid_module.uuid4()), role=role).save()
    except NotUniqueError:
        # A concurrent identical join won the race — still idempotent success.
        return jsonify({'joined': False, 'team_code': presented_code}), 200
    ensure_team_roles(team_code)
    return jsonify({'joined': True, 'team_code': presented_code}), 201


# LEAVE a team
@memberships_blueprint.route('/devices/<identifier>/teams/<code>', methods=['DELETE'])
@auth.login_required
def leave_team(identifier, code):
    denial = actor_denial(identifier)
    if denial:
        return denial

    device_id = str(identifier).lower()
    team_code = canonical_team_code(code) or code
    DeviceTeamMembership.objects(device_id=device_id, team_code=team_code).delete()
    JoinRequest.objects(device_id=device_id, team_code=team_code).delete()
    reassign_captain(team_code)
    return '', 204


# LIST memberships
@memberships_blueprint.route('/devices/<identifier>/teams', methods=['GET'])
@auth.login_required
def list_teams(identifier):
    denial = actor_denial(identifier)
    if denial:
        return denial

    memberships = DeviceTeamMembership.objects(
        device_id=str(identifier).lower(), removed_at=None).order_by('joined_at')
    teams = []
    for membership in memberships:
        team = Team.objects(code=membership.team_code).first()
        teams.append({
            'team_code': membership.team_code,
            'team_name': team.name if team else None,
            'archived': bool(team.archived) if team else False,
            'joined_at': membership.joined_at.strftime(TIMESTAMP_FORMAT)
            if membership.joined_at else None,
        })
    return jsonify({'teams': teams})


# ISSUE the per-device secret, once (V3 §1)
@memberships_blueprint.route('/devices/<identifier>/secret', methods=['POST'])
@auth.login_required
def issue_device_secret(identifier):
    denial = actor_denial(identifier)
    if denial:
        return denial

    device_id = str(identifier).lower()
    if DeviceSecret.objects(device_id=device_id).first() is not None:
        return jsonify({'reason': 'secret_already_issued'}), 409

    secret = secrets.token_urlsafe(32)
    try:
        DeviceSecret(device_id=device_id, secret_hash=hash_device_secret(secret)).save()
    except NotUniqueError:
        return jsonify({'reason': 'secret_already_issued'}), 409
    return jsonify({'secret': secret}), 201
