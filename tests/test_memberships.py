"""Tests for match_tracks.memberships (V2 §6): join/leave/list teams,
actor-rule authorization (including admin + X-Device-ID semantics from
match_tracks.auth.effective_device_id), and the is_member helper.

Device uuids and team codes in this file are prefixed with ``memtest-`` to
avoid colliding with fixtures created by other test files running in the
same process.
"""

import uuid as uuid_module
from datetime import datetime

from flask import jsonify

from match_tracks.auth import auth, effective_device_id
from match_tracks.memberships import is_member
from match_tracks.models import DeviceTeamMembership, Player, Team


def _register_effective_device_id_route(app):
    """Register a throwaway route that echoes effective_device_id()."""
    name = f'effective_device_id_route_{uuid_module.uuid4().hex}'
    path = f'/__test__/{name}/'

    def view():
        return jsonify({'effective_device_id': effective_device_id()})

    view.__name__ = name
    handler = auth.login_required(view)
    app.add_url_rule(path, endpoint=name, view_func=handler, methods=['GET'])
    return path


# --- join / rejoin --------------------------------------------------------

def test_join_team_creates_membership_and_auto_creates_team_stub(client, device_key):
    device_uuid = 'memtest-join-device'
    headers = device_key('memtest-join-key', device_uuid)
    team_code = 'memtest-join-team'

    response = client.post(f'/devices/{device_uuid}/teams', json={'team_code': team_code},
                            headers=headers)
    assert response.status_code == 201
    assert response.get_json()['joined'] is True

    membership = DeviceTeamMembership.objects(device_id=device_uuid, team_code=team_code).first()
    assert membership is not None

    team = Team.objects(code=team_code).first()
    assert team is not None
    assert team.name is None


def test_rejoin_team_returns_200_joined_false(client, device_key):
    device_uuid = 'memtest-rejoin-device'
    headers = device_key('memtest-rejoin-key', device_uuid)
    team_code = 'memtest-rejoin-team'

    first_response = client.post(f'/devices/{device_uuid}/teams', json={'team_code': team_code},
                                  headers=headers)
    assert first_response.status_code == 201

    second_response = client.post(f'/devices/{device_uuid}/teams', json={'team_code': team_code},
                                   headers=headers)
    assert second_response.status_code == 200
    assert second_response.get_json()['joined'] is False


def test_join_team_missing_team_code_returns_400(client, device_key):
    device_uuid = 'memtest-missing-team-code-device'
    headers = device_key('memtest-missing-team-code-key', device_uuid)

    response = client.post(f'/devices/{device_uuid}/teams', json={}, headers=headers)
    assert response.status_code == 400


# --- list ------------------------------------------------------------------

def test_list_teams_returns_team_name_and_joined_at_sorted(client, device_key):
    device_uuid = 'memtest-list-device'
    headers = device_key('memtest-list-key', device_uuid)

    Team(code='memtest-list-team-a', name='Team Alpha').save()
    Team(code='memtest-list-team-b', name='Team Beta').save()

    assert client.post(f'/devices/{device_uuid}/teams',
                        json={'team_code': 'memtest-list-team-a'}, headers=headers).status_code == 201
    assert client.post(f'/devices/{device_uuid}/teams',
                        json={'team_code': 'memtest-list-team-b'}, headers=headers).status_code == 201

    # Force a deterministic joined_at ordering regardless of clock resolution.
    membership_a = DeviceTeamMembership.objects(device_id=device_uuid,
                                                 team_code='memtest-list-team-a').first()
    membership_a.joined_at = datetime(2026, 1, 1)
    membership_a.save()
    membership_b = DeviceTeamMembership.objects(device_id=device_uuid,
                                                 team_code='memtest-list-team-b').first()
    membership_b.joined_at = datetime(2026, 2, 1)
    membership_b.save()

    list_response = client.get(f'/devices/{device_uuid}/teams', headers=headers)
    assert list_response.status_code == 200
    teams = list_response.get_json()['teams']

    assert [team['team_code'] for team in teams] == ['memtest-list-team-a', 'memtest-list-team-b']
    assert teams[0]['team_name'] == 'Team Alpha'
    assert teams[1]['team_name'] == 'Team Beta'
    assert teams[0]['joined_at'] == '2026-01-01T00:00:00Z'
    assert teams[1]['joined_at'] == '2026-02-01T00:00:00Z'


# --- delete ------------------------------------------------------------------

def test_delete_membership_removes_and_is_idempotent(client, device_key):
    device_uuid = 'memtest-delete-device'
    headers = device_key('memtest-delete-key', device_uuid)
    team_code = 'memtest-delete-team'

    join_response = client.post(f'/devices/{device_uuid}/teams', json={'team_code': team_code},
                                 headers=headers)
    assert join_response.status_code == 201

    first_delete = client.delete(f'/devices/{device_uuid}/teams/{team_code}', headers=headers)
    assert first_delete.status_code == 204
    assert DeviceTeamMembership.objects(device_id=device_uuid, team_code=team_code).first() is None

    second_delete = client.delete(f'/devices/{device_uuid}/teams/{team_code}', headers=headers)
    assert second_delete.status_code == 204


# --- actor rule --------------------------------------------------------------

def test_actor_rule_forbids_other_device_for_post_get_delete(client, device_key):
    device_a_headers = device_key('memtest-actor-a-key', 'memtest-actor-a')
    device_b_uuid = 'memtest-actor-b'

    post_response = client.post(f'/devices/{device_b_uuid}/teams',
                                 json={'team_code': 'memtest-actor-team'}, headers=device_a_headers)
    assert post_response.status_code == 403
    assert post_response.get_json() == {'reason': 'not_your_device'}

    get_response = client.get(f'/devices/{device_b_uuid}/teams', headers=device_a_headers)
    assert get_response.status_code == 403
    assert get_response.get_json() == {'reason': 'not_your_device'}

    delete_response = client.delete(f'/devices/{device_b_uuid}/teams/memtest-actor-team',
                                     headers=device_a_headers)
    assert delete_response.status_code == 403
    assert delete_response.get_json() == {'reason': 'not_your_device'}


def test_actor_rule_admin_may_act_for_any_device(client, admin_headers):
    device_uuid = 'memtest-actor-admin-target'
    response = client.post(f'/devices/{device_uuid}/teams',
                            json={'team_code': 'memtest-actor-admin-team'}, headers=admin_headers)
    assert response.status_code == 201


def test_effective_device_id_admin_with_x_device_id_header_acts_as_that_device(
        app, client, admin_headers):
    path = _register_effective_device_id_route(app)

    response = client.get(path, headers={**admin_headers, 'X-Device-ID': 'MEMTEST-HEADER-DEVICE'})
    assert response.status_code == 200
    assert response.get_json()['effective_device_id'] == 'memtest-header-device'


def test_effective_device_id_admin_without_header_is_none(app, client, admin_headers):
    path = _register_effective_device_id_route(app)

    response = client.get(path, headers=admin_headers)
    assert response.get_json()['effective_device_id'] is None


def test_effective_device_id_device_key_header_mismatch_returns_none(app, client, device_key):
    path = _register_effective_device_id_route(app)
    headers = device_key('memtest-mismatch-key', 'memtest-real-device')

    response = client.get(path, headers={**headers, 'X-Device-ID': 'memtest-other-device'})
    assert response.get_json()['effective_device_id'] is None


def test_effective_device_id_device_key_header_agrees(app, client, device_key):
    path = _register_effective_device_id_route(app)
    headers = device_key('memtest-agree-key', 'memtest-agree-device')

    response = client.get(path, headers={**headers, 'X-Device-ID': 'memtest-agree-device'})
    assert response.get_json()['effective_device_id'] == 'memtest-agree-device'


# --- is_member ---------------------------------------------------------------

def test_is_member_true_via_membership_row(app):
    with app.app_context():
        DeviceTeamMembership(device_id='memtest-ismember-row', team_code='memtest-ismember-team').save()
        assert is_member('memtest-ismember-row', 'memtest-ismember-team') is True


def test_is_member_true_via_legacy_player_team_code_only(app):
    with app.app_context():
        Player(device_id='memtest-ismember-legacy', team_code='memtest-ismember-legacy-team').save()
        assert is_member('memtest-ismember-legacy', 'memtest-ismember-legacy-team') is True


def test_is_member_false_when_no_membership_or_legacy_team(app):
    with app.app_context():
        assert is_member('memtest-ismember-none', 'memtest-ismember-none-team') is False
