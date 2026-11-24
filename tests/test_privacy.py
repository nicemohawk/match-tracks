"""Tests for match_tracks.privacy (V2 §5): display-name rendering, guardian
consent, consent-gated rosters, explicit team creation, and the full device
data-deletion cascade.

Identifiers in this file are prefixed with ``privtest-`` (except where the
legacy ``/devices/<uuid:...>/`` route requires an RFC-4122 uuid) to avoid
colliding with fixtures created by other test files running in the same
process.
"""

from datetime import datetime, timedelta, timezone

from match_tracks import field_service
from match_tracks.models import (CommunityField, Device, DeviceTeamMembership, Entitlement,
                                 LiveStatus, Match, MatchComment, Player, Team)
from match_tracks.privacy import consent_blocked, display_name
from tests.helpers import new_uuid, register_device


# --- display_name ------------------------------------------------------------

def test_display_name_renders_initials_for_full_name():
    assert display_name('Ben Lachman', True) == 'B. L.'


def test_display_name_renders_initials_for_single_name():
    assert display_name('Ben', True) == 'B.'


def test_display_name_none_input_returns_none():
    assert display_name(None, True) is None


def test_display_name_unchanged_when_not_initials_only():
    assert display_name('Ben Lachman', False) == 'Ben Lachman'


# --- consent -------------------------------------------------------------

def test_consent_acknowledged_true_returns_200_and_creates_player(client, device_key):
    device_uuid = 'privtest-consent-device'
    headers = device_key('privtest-consent-key', device_uuid)

    response = client.post(f'/devices/{device_uuid}/consent',
                            json={'guardian_name': 'Guardian G', 'acknowledged': True},
                            headers=headers)
    assert response.status_code == 200
    body = response.get_json()
    assert body['acknowledged_at']

    player = Player.objects(device_id=device_uuid).first()
    assert player is not None
    assert player.consent_acknowledged_at is not None


def test_consent_false_returns_400(client, device_key):
    device_uuid = 'privtest-consent-false-device'
    headers = device_key('privtest-consent-false-key', device_uuid)

    response = client.post(f'/devices/{device_uuid}/consent',
                            json={'acknowledged': False}, headers=headers)
    assert response.status_code == 400
    assert response.get_json() == {'reason': 'not_acknowledged'}


def test_consent_missing_acknowledged_returns_400(client, device_key):
    device_uuid = 'privtest-consent-missing-device'
    headers = device_key('privtest-consent-missing-key', device_uuid)

    response = client.post(f'/devices/{device_uuid}/consent', json={}, headers=headers)
    assert response.status_code == 400
    assert response.get_json() == {'reason': 'not_acknowledged'}


def test_consent_actor_rule_forbids_other_device(client, device_key):
    device_a_headers = device_key('privtest-consent-a-key', 'privtest-consent-a')

    response = client.post('/devices/privtest-consent-b/consent',
                            json={'acknowledged': True}, headers=device_a_headers)
    assert response.status_code == 403
    assert response.get_json() == {'reason': 'not_your_device'}


# --- POST /teams ---------------------------------------------------------

def test_create_team_new_stores_requires_consent(client, admin_headers):
    team_code = 'privtest-team-new'

    response = client.post('/teams', json={'code': team_code, 'name': 'Privacy Team',
                                            'requires_consent': True}, headers=admin_headers)
    assert response.status_code == 201
    body = response.get_json()['team']
    assert body == {'code': team_code, 'name': 'Privacy Team', 'requires_consent': True}


def test_create_team_replay_returns_200_and_does_not_overwrite(client, admin_headers):
    team_code = 'privtest-team-replay'

    first = client.post('/teams', json={'code': team_code, 'name': 'Original Name',
                                         'requires_consent': True}, headers=admin_headers)
    assert first.status_code == 201

    second = client.post('/teams', json={'code': team_code, 'name': 'Changed Name',
                                          'requires_consent': False}, headers=admin_headers)
    assert second.status_code == 200
    body = second.get_json()['team']
    assert body['name'] == 'Original Name'
    assert body['requires_consent'] is True


def test_create_team_missing_code_returns_400(client, admin_headers):
    response = client.post('/teams', json={'name': 'No Code'}, headers=admin_headers)
    assert response.status_code == 400


def test_create_team_allows_device_key_auth(client, device_key):
    headers = device_key('privtest-team-device-key', 'privtest-team-device')

    response = client.post('/teams', json={'code': 'privtest-team-any-key'}, headers=headers)
    assert response.status_code == 201


# --- consent_blocked -------------------------------------------------------

def test_consent_blocked_truth_table():
    open_team = Team(code='privtest-open-team', requires_consent=False)
    gated_team = Team(code='privtest-gated-team', requires_consent=True)

    consented_player = Player(device_id='privtest-consented', consent_acknowledged_at=datetime.now(timezone.utc))
    unconsented_player = Player(device_id='privtest-unconsented')

    assert consent_blocked(unconsented_player, open_team) is False
    assert consent_blocked(None, open_team) is False
    assert consent_blocked(unconsented_player, gated_team) is True
    assert consent_blocked(None, gated_team) is True
    assert consent_blocked(consented_player, gated_team) is False


# --- DELETE /devices/<device_id> cascade ------------------------------------
#
# NOTE (contract deviation): routes.py registers a legacy
# `DELETE /devices/<uuid:identifier>/` (coordinator-owned, see delete_device in
# match_tracks/routes.py) alongside privacy.py's `DELETE /devices/<device_id>`
# (no uuid constraint). With app.url_map.strict_slashes = False, Werkzeug
# resolves any RFC-4122-uuid-shaped path to the *legacy* rule (confirmed via
# app.url_map.bind(...).match(...)), which only deletes the Device row and
# returns 200 `{"deleted_device": ...}` — never touching Matches, CommunityField
# observations, comments, LiveStatus, memberships, Player, or Entitlements, and
# never returning the 202 `{"deletion_id": ...}` the V2 contract (§5) requires.
# Since device ids are documented as lowercase uuid strings everywhere else in
# the architecture, this means the compliant cascade delete is unreachable for
# real device ids. See test_delete_device_cascade_is_unreachable_for_uuid_ids
# below (left failing on purpose) for a minimal repro. This test instead uses a
# non-uuid-shaped device id so it reaches privacy.py's handler and the cascade
# LOGIC itself (which is correct) can be verified.

def test_delete_device_cascades_across_all_related_data(client, admin_headers, device_key):
    device_uuid = 'privtest-cascade-device'
    device_headers = device_key('privtest-cascade-key', device_uuid)
    register_device(client, admin_headers, vendor_identifier=device_uuid, name='Cascade Device')

    team_code = 'privtest-cascade-team'
    Team(code=team_code).save()

    Match(uuid=new_uuid(), device_id=device_uuid, team_code=team_code,
          recorded_at=datetime.now(timezone.utc), track={'coordinates': []}, events=[], stats={}).save()
    Match(uuid=new_uuid(), device_id=device_uuid, team_code=team_code,
          recorded_at=datetime.now(timezone.utc), track={'coordinates': []}, events=[], stats={}).save()
    assert Match.objects(device_id=device_uuid).count() == 2

    other_contributor = new_uuid()
    initial_confidence = field_service.compute_confidence(field_service.CONFIDENCE_BASE_INFERRED, 3)
    field = CommunityField(
        uuid=new_uuid(),
        device_id=other_contributor,
        rect_center_lat=39.0,
        rect_center_lon=-82.0,
        rect_length_m=100.0,
        rect_width_m=64.0,
        rect_heading_deg=10.0,
        source='community',
        observation_count=3,
        confidence=initial_confidence,
        has_trained_observation=False,
        contributing_device_ids=[device_uuid, other_contributor],
    )
    field.save()

    comment_uuid = new_uuid()
    MatchComment(uuid=comment_uuid, match_uuid=new_uuid(), team_code=team_code,
                 author_device=device_uuid, author_name='Cascade Kid', body='hello team').save()

    LiveStatus(device_id=device_uuid, team_code=team_code, match_uuid=new_uuid()).save()
    DeviceTeamMembership(device_id=device_uuid, team_code=team_code).save()
    Player(device_id=device_uuid, name='Cascade Kid', team_code=team_code).save()
    Entitlement(device_id=device_uuid, product_id='com.nicemohawk.MatchTracker.team.monthly',
                expires_at=datetime.now(timezone.utc) + timedelta(days=30), environment='test').save()

    delete_response = client.delete(f'/devices/{device_uuid}', headers=device_headers)
    assert delete_response.status_code == 202
    assert delete_response.get_json()['deletion_id']

    assert Match.objects(device_id=device_uuid).count() == 0

    field.reload()
    assert field.observation_count == 2
    assert device_uuid not in field.contributing_device_ids
    assert field.rect_center_lat == 39.0
    assert field.rect_center_lon == -82.0
    assert field.rect_length_m == 100.0
    assert field.rect_width_m == 64.0
    assert field.rect_heading_deg == 10.0
    assert field.confidence < initial_confidence

    comment = MatchComment.objects(uuid=comment_uuid).first()
    assert comment is not None
    assert comment.author_name == '[deleted]'
    assert comment.author_tombstoned is True

    assert LiveStatus.objects(device_id=device_uuid).count() == 0
    assert DeviceTeamMembership.objects(device_id=device_uuid).count() == 0
    assert Player.objects(device_id=device_uuid).count() == 0
    assert Entitlement.objects(device_id=device_uuid).count() == 0
    assert Device.objects(vendor_identifier=device_uuid).first() is None

    second_delete_response = client.delete(f'/devices/{device_uuid}', headers=device_headers)
    assert second_delete_response.status_code == 202


def test_delete_device_cascade_is_unreachable_for_uuid_shaped_device_ids(
        client, admin_headers, device_key):
    """Contract deviation repro (see NOTE above) — left failing on purpose.

    Per V2 §5, DELETE /devices/<device_id> must run the full cascade and
    respond 202 {"deletion_id": ...} for any device, and device ids are
    documented (auth.py, memberships.py, entitlements.py) as lowercase uuid
    strings. But routes.py's legacy `DELETE /devices/<uuid:identifier>/`
    shadows privacy.py's route for exactly those uuid-shaped ids, so real
    device ids never reach the V2 cascade at all.
    """
    device_uuid = new_uuid()
    device_headers = device_key('privtest-cascade-shadowed-key', device_uuid)
    register_device(client, admin_headers, vendor_identifier=device_uuid, name='Shadowed Device')

    delete_response = client.delete(f'/devices/{device_uuid}', headers=device_headers)
    assert delete_response.status_code == 202
    assert 'deletion_id' in delete_response.get_json()


def test_delete_device_actor_rule_forbids_other_device(client, device_key):
    device_a_headers = device_key('privtest-delete-a-key', 'privtest-delete-a')

    response = client.delete('/devices/privtest-delete-b', headers=device_a_headers)
    assert response.status_code == 403
    assert response.get_json() == {'reason': 'not_your_device'}


def test_delete_device_actor_rule_allows_admin(client, admin_headers):
    response = client.delete('/devices/privtest-delete-admin-target', headers=admin_headers)
    assert response.status_code == 202
    assert response.get_json()['deletion_id']
