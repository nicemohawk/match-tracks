"""Tests for match_tracks.team_admin (proactive team lifecycle management).

Covers the frozen contract in docs/team-management-contract.md: creator-owned
teams, owner/admin-gated rename & consent toggles, reversible soft-archive
(DELETE), ownership claim of auto-created stubs, and the archived-team join
guard. Identifiers are prefixed ``tadmin-`` to avoid colliding with fixtures
from other test files sharing the process.
"""

from match_tracks.models import DeviceTeamMembership, Player, Team
from tests.helpers import (make_extended_session_payload, make_match_track,
                           new_uuid, register_device)

VENUE_LAT, VENUE_LON = 39.0, -82.0


def _own_team(client, device_key, membership, code, name='Owners FC',
              key='tadmin-owner-key', device='tadmin-owner'):
    """Create a team owned by a device that is also a member, return its headers."""
    headers = device_key(key, device)
    response = client.post('/teams', json={'code': code, 'name': name}, headers=headers)
    assert response.status_code == 201, response.get_data(as_text=True)
    assert response.get_json()['team']['code'] == code
    membership(device, code)  # owner joins their own team so GET is permitted
    return headers


# 1. Device creates a team → owner set; is_owner true for owner, false for member.
def test_create_sets_owner_and_get_reports_is_owner(client, device_key, membership):
    code = 'tadmin-owned-1'
    owner_headers = _own_team(client, device_key, membership, code)

    team = Team.objects(code=code).first()
    assert team.owner_device_id == 'tadmin-owner'
    assert team.archived is False

    owner_view = client.get(f'/teams/{code}', headers=owner_headers)
    assert owner_view.status_code == 200
    assert owner_view.get_json()['team']['is_owner'] is True
    assert 'owner_device_id' not in owner_view.get_json()['team']  # never exposed

    member_headers = device_key('tadmin-m1-key', 'tadmin-m1')
    membership('tadmin-m1', code)
    member_view = client.get(f'/teams/{code}', headers=member_headers)
    assert member_view.status_code == 200
    assert member_view.get_json()['team']['is_owner'] is False


# 2. Non-owner PATCH → 403; owner PATCH renames → 200 and GET reflects it.
def test_patch_owner_only_and_renames(client, device_key, membership):
    code = 'tadmin-owned-2'
    owner_headers = _own_team(client, device_key, membership, code)

    stranger_headers = device_key('tadmin-s2-key', 'tadmin-s2')
    membership('tadmin-s2', code)
    forbidden = client.patch(f'/teams/{code}', json={'name': 'Hijacked'},
                             headers=stranger_headers)
    assert forbidden.status_code == 403
    assert forbidden.get_json() == {'reason': 'not_team_owner'}

    renamed = client.patch(f'/teams/{code}', json={'name': 'Renamed FC'},
                           headers=owner_headers)
    assert renamed.status_code == 200
    assert renamed.get_json()['team']['name'] == 'Renamed FC'

    reread = client.get(f'/teams/{code}', headers=owner_headers)
    assert reread.get_json()['team']['name'] == 'Renamed FC'


# 3. Owner DELETE archives; GET shows archived; stats still return; join → 409.
def test_delete_archives_but_preserves_stats_and_blocks_join(client, admin_headers,
                                                             device_key, membership):
    code = 'tadmin-owned-3'
    owner_headers = _own_team(client, device_key, membership, code)

    archived = client.delete(f'/teams/{code}', headers=owner_headers)
    assert archived.status_code == 200
    assert archived.get_json() == {'code': code, 'archived': True}

    team = Team.objects(code=code).first()
    assert team.archived is True
    assert team.archived_at is not None

    view = client.get(f'/teams/{code}', headers=owner_headers)
    assert view.get_json()['team']['archived'] is True

    # Stats endpoint still serves an archived team (upload/read never blocked).
    stats = client.get(f'/teams/{code}/stats', headers=admin_headers)
    assert stats.status_code == 200

    joiner_headers = device_key('tadmin-j3-key', 'tadmin-j3')
    blocked = client.post('/devices/tadmin-j3/teams', json={'team_code': code},
                          headers=joiner_headers)
    assert blocked.status_code == 409
    assert blocked.get_json() == {'reason': 'team_archived'}


# 4. PATCH {archived:false} unarchives; join allowed again.
def test_unarchive_reopens_joins(client, device_key, membership):
    code = 'tadmin-owned-4'
    owner_headers = _own_team(client, device_key, membership, code)

    client.delete(f'/teams/{code}', headers=owner_headers)
    unarchived = client.patch(f'/teams/{code}', json={'archived': False},
                              headers=owner_headers)
    assert unarchived.status_code == 200
    assert unarchived.get_json()['team']['archived'] is False
    assert Team.objects(code=code).first().archived_at is None

    joiner_headers = device_key('tadmin-j4-key', 'tadmin-j4')
    rejoined = client.post('/devices/tadmin-j4/teams', json={'team_code': code},
                           headers=joiner_headers)
    assert rejoined.status_code in (200, 201)


# 5. A member claims an unowned auto-created team; a second device claiming → 403.
def test_claim_unowned_then_second_claim_forbidden(client, device_key):
    code = 'tadmin-claimable'

    first_headers = device_key('tadmin-c1-key', 'tadmin-c1')
    # Joining an unknown team auto-creates an unowned stub and joins the device.
    joined = client.post('/devices/tadmin-c1/teams', json={'team_code': code},
                         headers=first_headers)
    assert joined.status_code == 201
    assert Team.objects(code=code).first().owner_device_id is None

    claim = client.post(f'/teams/{code}/claim', headers=first_headers)
    assert claim.status_code == 200
    assert claim.get_json() == {'claimed': True, 'is_owner': True}
    assert Team.objects(code=code).first().owner_device_id == 'tadmin-c1'

    # Re-claim by the owner is idempotent.
    reclaim = client.post(f'/teams/{code}/claim', headers=first_headers)
    assert reclaim.status_code == 200
    assert reclaim.get_json() == {'claimed': False, 'is_owner': True}

    # A second member cannot steal ownership.
    second_headers = device_key('tadmin-c2-key', 'tadmin-c2')
    client.post('/devices/tadmin-c2/teams', json={'team_code': code}, headers=second_headers)
    stolen = client.post(f'/teams/{code}/claim', headers=second_headers)
    assert stolen.status_code == 403
    assert stolen.get_json() == {'reason': 'already_owned'}


# 6. POST /teams owned by another device → 409; by the owner → 200 (updates name).
def test_post_teams_ownership_conflict_and_owner_update(client, device_key, membership):
    code = 'tadmin-owned-6'
    owner_headers = _own_team(client, device_key, membership, code, name='Original')

    other_headers = device_key('tadmin-o6-key', 'tadmin-o6')
    conflict = client.post('/teams', json={'code': code, 'name': 'Takeover'},
                           headers=other_headers)
    assert conflict.status_code == 409
    assert conflict.get_json() == {'reason': 'already_owned'}
    assert Team.objects(code=code).first().name == 'Original'

    updated = client.post('/teams', json={'code': code, 'name': 'New Name'},
                          headers=owner_headers)
    assert updated.status_code == 200
    assert updated.get_json()['team']['name'] == 'New Name'
    assert Team.objects(code=code).first().name == 'New Name'


# 7. Admin manages any team (PATCH/DELETE) regardless of owner.
def test_admin_manages_any_team(client, admin_headers, device_key, membership):
    code = 'tadmin-owned-7'
    _own_team(client, device_key, membership, code, name='Owned')

    renamed = client.patch(f'/teams/{code}', json={'name': 'Admin Renamed'},
                           headers=admin_headers)
    assert renamed.status_code == 200
    assert renamed.get_json()['team']['name'] == 'Admin Renamed'

    archived = client.delete(f'/teams/{code}', headers=admin_headers)
    assert archived.status_code == 200
    assert Team.objects(code=code).first().archived is True


# 8. GET: 404 unknown, 403 non-member, 200 member with member_count (2 devices).
def test_get_visibility_and_member_count(client, admin_headers, device_key):
    unknown = client.get('/teams/tadmin-nonexistent', headers=admin_headers)
    assert unknown.status_code == 404
    assert unknown.get_json() == {'reason': 'unknown_team'}

    code = 'tadmin-count'
    Team(code=code, name='Counted', owner_device_id='tadmin-count-owner').save()
    DeviceTeamMembership(device_id='tadmin-count-a', team_code=code).save()  # explicit member
    Player(device_id='tadmin-count-b', team_code=code).save()               # legacy default

    stranger_headers = device_key('tadmin-x8-key', 'tadmin-x8')
    non_member = client.get(f'/teams/{code}', headers=stranger_headers)
    assert non_member.status_code == 403
    assert non_member.get_json() == {'reason': 'not_a_member'}

    member_headers = device_key('tadmin-count-a-key', 'tadmin-count-a')
    view = client.get(f'/teams/{code}', headers=member_headers)
    assert view.status_code == 200
    assert view.get_json()['team']['member_count'] == 2


# 9. Auto-create intact: a session tagging a new team still 200, unowned/unarchived.
def test_session_still_autocreates_unowned_team(client, admin_headers, device_key):
    device_uuid = register_device(client, admin_headers)
    headers = device_key('tadmin-sess-key', device_uuid)
    code = 'tadmin-autocreate'

    payload = make_extended_session_payload(
        new_uuid(), '2026-07-13T09:00:00Z',
        make_match_track(VENUE_LAT, VENUE_LON, num_points=5),
        team_code=code, player_name='Auto Player')
    response = client.post(f'/devices/{device_uuid}/sessions/',
                           json={'sessions': [payload]}, headers=headers)
    assert response.status_code == 200

    team = Team.objects(code=code).first()
    assert team is not None
    assert team.owner_device_id is None
    assert team.archived is False
