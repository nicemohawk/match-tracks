"""Minors privacy & compliance (V2 §5): initials rendering, guardian consent,
consent-gated rosters, explicit team creation, and full device data deletion.
"""

import uuid as uuid_module
from datetime import timezone

from flask import Blueprint, current_app, jsonify, request
from mongoengine import NotUniqueError

from match_tracks import field_service
from match_tracks.auth import auth, effective_device_id
from match_tracks.memberships import actor_denial, principal_owns_team
from match_tracks.models import (CommunityField, Device, DeviceTeamMembership,
                                 Entitlement, LiveStatus, Match, MatchComment,
                                 Player, Team)
from match_tracks.timeutils import utcnow

privacy_blueprint = Blueprint('privacy', __name__)

TIMESTAMP_FORMAT = '%Y-%m-%dT%H:%M:%SZ'
TOMBSTONE_AUTHOR = '[deleted]'


def display_name(full_name, initials_only):
    """Render a player's name honoring the initials_only privacy flag.

    "Ben Lachman" with initials_only becomes "B. L."; without, unchanged.
    """
    if full_name is None:
        return None
    if not initials_only:
        return full_name
    initials = [word[0].upper() for word in full_name.split() if word]
    if not initials:
        return full_name
    return ' '.join(f'{letter}.' for letter in initials)


def player_display_name(player):
    """Display name for a Player row (None-safe)."""
    if player is None:
        return None
    return display_name(player.name, player.initials_only)


def consent_blocked(player, team):
    """True when the team requires guardian consent the player hasn't given."""
    if team is None or not team.requires_consent:
        return False
    return player is None or player.consent_acknowledged_at is None


# RECORD guardian consent
@privacy_blueprint.route('/devices/<identifier>/consent', methods=['POST'])
@auth.login_required
def record_consent(identifier):
    denial = actor_denial(identifier)
    if denial:
        return denial

    json_data = request.get_json(silent=True) or {}
    if json_data.get('acknowledged') is not True:
        return jsonify({'reason': 'not_acknowledged'}), 400

    device_id = str(identifier).lower()
    player = Player.objects(device_id=device_id).first() or Player(device_id=device_id)
    player.consent_acknowledged_at = utcnow()
    player.updated_at = utcnow()
    player.save()

    return jsonify({
        'acknowledged_at': player.consent_acknowledged_at.astimezone(
            timezone.utc).strftime(TIMESTAMP_FORMAT),
    })


# CREATE a team explicitly (the only way to set requires_consent)
@privacy_blueprint.route('/teams', methods=['POST'])
@auth.login_required
def create_team():
    json_data = request.get_json(silent=True) or {}
    code = json_data.get('code')
    if not code:
        return jsonify({'reason': 'code_required'}), 400

    # Actor is the acting device (None for an admin without X-Device-ID).
    actor = effective_device_id()
    existing = Team.objects(code=code).first()
    if existing is None:
        team = Team(code=code,
                    name=json_data.get('name'),
                    requires_consent=bool(json_data.get('requires_consent', False)),
                    owner_device_id=actor,
                    archived=False)
        try:
            team.save()
        except NotUniqueError:
            existing = Team.objects(code=code).first()  # lost a create race
        else:
            return jsonify({'team': _team_json(team)}), 201

    # Team already exists.
    owns = principal_owns_team(existing)
    unowned = existing.owner_device_id is None
    if not owns and not unowned:
        return jsonify({'reason': 'already_owned'}), 409

    # Caller owns it (owner device or admin) or it is unowned. Only an acting
    # device mutates: it adopts an unowned team and applies provided fields. An
    # admin without an X-Device-ID has no device context, so a bare replay just
    # returns the existing team unchanged (legacy idempotent behavior).
    if actor is not None:
        if unowned:
            existing.owner_device_id = actor
        if 'name' in json_data:
            existing.name = json_data.get('name')
        if 'requires_consent' in json_data:
            existing.requires_consent = bool(json_data.get('requires_consent'))
        existing.save()

    return jsonify({'team': _team_json(existing)}), 200


def _team_json(team):
    return {
        'code': team.code,
        'name': team.name,
        'requires_consent': bool(team.requires_consent),
    }


# DELETE a device and everything it contributed (compliance path — never rate-limited)
@privacy_blueprint.route('/devices/<device_id>', methods=['DELETE'])
@auth.login_required
def delete_device_data(device_id):
    denial = actor_denial(device_id)
    if denial:
        return denial

    normalized_device_id = str(device_id).lower()

    Match.objects(device_id=normalized_device_id).delete()

    # Field observations: decrement counts, keep the merged community geometry.
    for field in CommunityField.objects(contributing_device_ids=normalized_device_id):
        field.contributing_device_ids = [
            contributor for contributor in field.contributing_device_ids
            if contributor != normalized_device_id
        ]
        field.observation_count = max(1, (field.observation_count or 1) - 1)
        confidence_base = (field_service.CONFIDENCE_BASE_TRAINED
                           if field.has_trained_observation
                           else field_service.CONFIDENCE_BASE_INFERRED)
        field.confidence = field_service.compute_confidence(
            confidence_base, field.observation_count)
        field.save()

    MatchComment.objects(author_device=normalized_device_id).update(
        set__author_tombstoned=True, set__author_name=TOMBSTONE_AUTHOR)

    LiveStatus.objects(device_id=normalized_device_id).delete()
    DeviceTeamMembership.objects(device_id=normalized_device_id).delete()
    Player.objects(device_id=normalized_device_id).delete()
    Entitlement.objects(device_id=normalized_device_id).delete()
    Device.objects(vendor_identifier=normalized_device_id).delete()

    deletion_id = str(uuid_module.uuid4())
    current_app.logger.warning(
        'device data deletion: device=%s deletion_id=%s (backups purge within 30 days)',
        normalized_device_id, deletion_id)

    return jsonify({'deletion_id': deletion_id}), 202
