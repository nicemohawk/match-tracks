"""Acceptance tests for V3 captain-led teams (docs/BACKEND_UPGRADE_PROMPT_V3.md).

One test (at least) per bullet in the contract's "Acceptance tests" list, plus
the identity/secret, code-rotation and lineup/next mechanics. Identifiers are
prefixed ``v3-`` to avoid colliding with fixtures from other files sharing the
process.
"""

import json
from datetime import datetime, timedelta

from match_tracks.models import (DeviceSecret, DeviceTeamMembership, JoinRequest,
                                 MatchEvent, Player, Team, TeamLineup)
from tests.helpers import (make_extended_session_payload, make_match_track,
                           new_uuid)

VENUE_LAT, VENUE_LON = 40.0, -83.0


# --- helpers ---------------------------------------------------------------

def _secret_headers(client, device_key, key, device_uuid):
    """Device-scoped headers carrying a freshly issued X-Device-Secret."""
    headers = device_key(key, device_uuid)
    response = client.post(f'/devices/{device_uuid}/secret', headers=headers)
    assert response.status_code == 201, response.get_data(as_text=True)
    return {**headers, 'X-Device-Secret': response.get_json()['secret']}


def _join(client, headers, device_uuid, team_code):
    return client.post(f'/devices/{device_uuid}/teams',
                       json={'team_code': team_code}, headers=headers)


def _member_id(device_uuid, team_code):
    row = DeviceTeamMembership.objects(
        device_id=device_uuid.lower(), team_code=team_code).first()
    return row.member_id if row else None


def _upload(client, headers, device_uuid, team_code, player_name='P',
            stats=None, events=None, recorded_at='2026-07-13T09:00:00Z'):
    payload = make_extended_session_payload(
        new_uuid(), recorded_at, make_match_track(VENUE_LAT, VENUE_LON, num_points=6),
        team_code=team_code, player_name=player_name, duration_s=5400.0,
        stats=stats, events=events)
    return client.post(f'/devices/{device_uuid}/sessions/',
                       json={'sessions': [payload]}, headers=headers)


# --- §1 identity -----------------------------------------------------------

def test_device_secret_issued_once(client, device_key):
    headers = device_key('v3-sec-key', 'v3-sec-dev')
    first = client.post('/devices/v3-sec-dev/secret', headers=headers)
    assert first.status_code == 201
    assert first.get_json()['secret']
    assert DeviceSecret.objects(device_id='v3-sec-dev').count() == 1

    second = client.post('/devices/v3-sec-dev/secret', headers=headers)
    assert second.status_code == 409


def test_write_requires_device_secret(client, device_key):
    headers = device_key('v3-nosec-key', 'v3-nosec-dev')
    _join(client, headers, 'v3-nosec-dev', 'v3-nosec-team')  # captain, no secret yet
    # PUT name without the X-Device-Secret header → 401.
    resp = client.put('/teams/v3-nosec-team', json={'name': 'X'}, headers=headers)
    assert resp.status_code == 401
    assert resp.get_json() == {'reason': 'device_secret_required'}

    # Wrong secret → 403.
    client.post('/devices/v3-nosec-dev/secret', headers=headers)
    resp = client.put('/teams/v3-nosec-team', json={'name': 'X'},
                      headers={**headers, 'X-Device-Secret': 'wrong'})
    assert resp.status_code == 403
    assert resp.get_json() == {'reason': 'device_secret_invalid'}


# --- Acceptance #1 ---------------------------------------------------------

def test_joined_without_uploads_can_read_and_is_a_member(client, device_key):
    captain = _secret_headers(client, device_key, 'v3-r1-cap-key', 'v3-r1-cap')
    member = _secret_headers(client, device_key, 'v3-r1-mem-key', 'v3-r1-mem')
    code = 'v3-read-team'
    assert _join(client, captain, 'v3-r1-cap', code).status_code == 201
    assert _join(client, member, 'v3-r1-mem', code).status_code == 201

    # The watchless teammate reads every team view without being denied.
    assert client.get(f'/teams/{code}/stats', headers=member).status_code == 200
    assert client.get(f'/teams/{code}/events', headers=member).status_code == 200
    assert client.get(f'/teams/{code}/live', headers=member).status_code == 200
    formation = client.get(f'/teams/{code}/formation', headers=member)
    assert formation.status_code in (200, 404)  # not a membership denial
    if formation.status_code == 404:
        assert formation.get_json()['reason'] == 'insufficient_data'

    team_view = client.get(f'/teams/{code}', headers=member)
    assert team_view.status_code == 200
    member_ids = {m['member_id'] for m in team_view.get_json()['members']}
    assert _member_id('v3-r1-mem', code) in member_ids


# --- Acceptance #2 ---------------------------------------------------------

def test_first_joiner_is_captain_second_is_player_no_coach(client, device_key):
    cap = _secret_headers(client, device_key, 'v3-c2-key', 'v3-c2-cap')
    pl = _secret_headers(client, device_key, 'v3-p2-key', 'v3-p2-player')
    code = 'v3-roles-team'
    assert _join(client, cap, 'v3-c2-cap', code).status_code == 201
    assert _join(client, pl, 'v3-p2-player', code).status_code == 201

    view = client.get(f'/teams/{code}', headers=cap).get_json()
    roles = {m['member_id']: m['role'] for m in view['members']}
    assert roles[_member_id('v3-c2-cap', code)] == 'captain'
    assert roles[_member_id('v3-p2-player', code)] == 'player'
    assert sum(1 for role in roles.values() if role == 'captain') == 1
    assert 'coach' not in roles.values()  # no coach exists, and nothing 4xx'd


# --- Acceptance #3 ---------------------------------------------------------

def test_only_captain_may_rename_and_everyone_sees_it(client, device_key):
    cap = _secret_headers(client, device_key, 'v3-c3-key', 'v3-c3-cap')
    pl = _secret_headers(client, device_key, 'v3-p3-key', 'v3-p3-player')
    code = 'v3-rename-team'
    _join(client, cap, 'v3-c3-cap', code)
    _join(client, pl, 'v3-p3-player', code)

    forbidden = client.put(f'/teams/{code}', json={'name': 'Hijack'}, headers=pl)
    assert forbidden.status_code == 403
    assert forbidden.get_json() == {'reason': 'not_captain'}

    renamed = client.put(f'/teams/{code}', json={'name': 'Red Dragons'}, headers=cap)
    assert renamed.status_code == 200

    assert client.get(f'/teams/{code}', headers=pl).get_json()['name'] == 'Red Dragons'


def test_invalid_team_name_rejected(client, device_key):
    cap = _secret_headers(client, device_key, 'v3-nm-key', 'v3-nm-cap')
    code = 'v3-name-team'
    _join(client, cap, 'v3-nm-cap', code)
    for bad in ('', 'x' * 61, 'two\nlines'):
        resp = client.put(f'/teams/{code}', json={'name': bad}, headers=cap)
        assert resp.status_code == 400
        assert resp.get_json() == {'reason': 'invalid_name'}


# --- Acceptance #4 ---------------------------------------------------------

def test_removed_member_blocked_until_restored(client, device_key):
    cap = _secret_headers(client, device_key, 'v3-c4-key', 'v3-c4-cap')
    victim = _secret_headers(client, device_key, 'v3-v4-key', 'v3-v4-dev')
    code = 'v3-remove-team'
    _join(client, cap, 'v3-c4-cap', code)
    _join(client, victim, 'v3-v4-dev', code)
    victim_member_id = _member_id('v3-v4-dev', code)

    removed = client.delete(f'/teams/{code}/members/{victim_member_id}', headers=cap)
    assert removed.status_code == 204

    # Reads are refused with reason "removed".
    read = client.get(f'/teams/{code}', headers=victim)
    assert read.status_code == 403
    assert read.get_json() == {'reason': 'removed'}
    assert client.get(f'/teams/{code}/stats', headers=victim).status_code == 403

    # Uploads tagging the team are refused with not_a_member.
    upload = _upload(client, victim, 'v3-v4-dev', code)
    assert upload.status_code == 400
    assert upload.get_json() == {'reason': 'not_a_member'}

    # Rejoining the same code is refused with reason "removed".
    rejoin = _join(client, victim, 'v3-v4-dev', code)
    assert rejoin.status_code == 403
    assert rejoin.get_json() == {'reason': 'removed'}

    # Restore → everything works again.
    restored = client.post(f'/teams/{code}/members/{victim_member_id}/restore', headers=cap)
    assert restored.status_code == 204
    assert client.get(f'/teams/{code}', headers=victim).status_code == 200
    assert _upload(client, victim, 'v3-v4-dev', code).status_code == 200


def test_removed_members_matches_leave_the_totals(client, device_key):
    cap = _secret_headers(client, device_key, 'v3-c4b-key', 'v3-c4b-cap')
    member = _secret_headers(client, device_key, 'v3-m4b-key', 'v3-m4b-dev')
    code = 'v3-totals-team'
    _join(client, cap, 'v3-c4b-cap', code)
    _join(client, member, 'v3-m4b-dev', code)
    _upload(client, member, 'v3-m4b-dev', code, player_name='Mover')

    before = client.get(f'/teams/{code}/stats', headers=cap).get_json()['players']
    assert any(p['player_name'] == 'Mover' for p in before)

    client.delete(f'/teams/{code}/members/{_member_id("v3-m4b-dev", code)}', headers=cap)
    after = client.get(f'/teams/{code}/stats', headers=cap).get_json()['players']
    assert not any(p['player_name'] == 'Mover' for p in after)


# --- Acceptance #5 ---------------------------------------------------------

def test_captain_deletion_promotes_longest_standing_member(client, device_key):
    cap = _secret_headers(client, device_key, 'v3-c5-key', 'v3-c5-cap')
    second = _secret_headers(client, device_key, 'v3-s5-key', 'v3-s5-dev')
    third = _secret_headers(client, device_key, 'v3-t5-key', 'v3-t5-dev')
    code = 'v3-handover-team'
    _join(client, cap, 'v3-c5-cap', code)
    _join(client, second, 'v3-s5-dev', code)
    _join(client, third, 'v3-t5-dev', code)

    # Pin a deterministic joined_at order: second is older than third.
    DeviceTeamMembership.objects(device_id='v3-s5-dev', team_code=code).update(
        set__joined_at=datetime(2026, 1, 1))
    DeviceTeamMembership.objects(device_id='v3-t5-dev', team_code=code).update(
        set__joined_at=datetime(2026, 2, 1))

    deleted = client.delete('/devices/v3-c5-cap', headers=cap)
    assert deleted.status_code == 202

    # The longest-standing remaining member (second) is now captain.
    assert DeviceTeamMembership.objects(
        device_id='v3-s5-dev', team_code=code).first().role == 'captain'
    assert DeviceTeamMembership.objects(device_id='v3-c5-cap', team_code=code).first() is None


# --- Acceptance #6 ---------------------------------------------------------

def test_live_free_but_annotation_needs_entitlement(app, client, device_key):
    app.config['ENTITLEMENTS_ENFORCED'] = True
    member = _secret_headers(client, device_key, 'v3-e6-key', 'v3-e6-dev')
    code = 'v3-entitle-team'
    _join(client, member, 'v3-e6-dev', code)

    live = client.post('/devices/v3-e6-dev/live', headers=member,
                       json={'team_code': code, 'match_uuid': 'v3m6', 'sequence': 1})
    assert live.status_code == 204

    MatchEvent(event_uuid='v3-ev6', match_uuid='v3m6', team_code=code,
               device_id='v3-e6-dev', kind='goalMine', date=datetime.utcnow(),
               provisional=False).save()
    annotation = client.post('/matches/v3m6/events/v3-ev6/annotation',
                             json={'label': 'great run'}, headers=member)
    assert annotation.status_code == 402
    assert annotation.get_json() == {'reason': 'entitlement_required'}


# --- Acceptance #7 ---------------------------------------------------------

def test_no_private_fields_about_other_players(client, device_key):
    cap = _secret_headers(client, device_key, 'v3-c7-key', 'v3-c7-cap')
    other = _secret_headers(client, device_key, 'v3-o7-key', 'v3-o7-dev')
    code = 'v3-privacy-team'
    _join(client, cap, 'v3-c7-cap', code)
    _join(client, other, 'v3-o7-dev', code)

    stats = make_extended_session_payload(
        new_uuid(), '2026-07-13T09:00:00Z',
        make_match_track(VENUE_LAT, VENUE_LON, num_points=6),
        team_code=code, player_name='Other',
        duration_s=5400.0,
        stats={'total_distance_m': 9000.0, 'time_on_pitch_s': 5000.0,
               'sprint_count': 12, 'avg_hr': 165.0, 'workrate_score': 7.0},
        events=[{'uuid': new_uuid(), 'kind': 'goalMine', 'date': '2026-07-13T09:20:00Z'}])
    assert client.post('/devices/v3-o7-dev/sessions/',
                       json={'sessions': [stats]}, headers=other).status_code == 200
    client.post('/devices/v3-o7-dev/live', headers=other,
                json={'team_code': code, 'match_uuid': 'v3m7', 'sequence': 1,
                      'heart_rate': 170.0, 'distance_m': 1000.0, 'x': 0.5, 'y': 0.5})

    for path in (f'/teams/{code}/stats', f'/teams/{code}/live', f'/teams/{code}/events',
                 f'/teams/{code}/matches', f'/teams/{code}'):
        blob = json.dumps(client.get(path, headers=cap).get_json())
        assert 'v3-o7-dev' not in blob, f'device id leaked in {path}'
        assert 'heart_rate' not in blob, f'heart_rate leaked in {path}'
        assert 'avg_hr' not in blob, f'avg_hr leaked in {path}'
        assert 'avg_workrate_score' not in blob, f'workrate leaked in {path}'
        assert 'coordinates' not in blob, f'route leaked in {path}'
        assert '"track"' not in blob, f'track leaked in {path}'


# --- Acceptance #8 ---------------------------------------------------------

def test_initials_only_enforced_everywhere(client, device_key):
    cap = _secret_headers(client, device_key, 'v3-i8-key', 'v3-i8-cap')
    code = 'v3-initials-team'
    _join(client, cap, 'v3-i8-cap', code)
    Player.objects(device_id='v3-i8-cap').update_one(
        set__name='Ben Lachman', set__initials_only=True, upsert=True)
    _upload(client, cap, 'v3-i8-cap', code, player_name='Ben Lachman')
    client.post('/devices/v3-i8-cap/live', headers=cap,
                json={'team_code': code, 'match_uuid': 'v3m8', 'sequence': 1})
    # updated_by on the lineup must also be initials-only.
    client.put(f'/teams/{code}/lineup', headers=cap,
               json={'name': '4-3-3', 'slots': [{'member_id': _member_id('v3-i8-cap', code),
                                                 'x': 0.5, 'y': 0.1}]})

    members = client.get(f'/teams/{code}', headers=cap).get_json()['members']
    assert members[0]['player_name'] == 'B. L.'
    stats = client.get(f'/teams/{code}/stats', headers=cap).get_json()['players']
    assert stats[0]['player_name'] == 'B. L.'
    live = client.get(f'/teams/{code}/live', headers=cap).get_json()['players']
    assert live[0]['player_name'] == 'B. L.'
    lineup = client.get(f'/teams/{code}/lineup', headers=cap).get_json()
    assert lineup['updated_by'] == 'B. L.'


def test_requires_consent_holds_joins_pending_and_hides_members(client, admin_headers, device_key):
    code = 'v3-consent-team'
    client.post('/teams', json={'code': code, 'requires_consent': True}, headers=admin_headers)

    cap = _secret_headers(client, device_key, 'v3-cc-key', 'v3-cc-cap')
    kid = _secret_headers(client, device_key, 'v3-kid-key', 'v3-kid-dev')
    # First join becomes captain directly; later joins are pending.
    assert _join(client, cap, 'v3-cc-cap', code).status_code == 201
    pending = _join(client, kid, 'v3-kid-dev', code)
    assert pending.status_code == 202
    assert pending.get_json()['pending'] is True
    assert DeviceTeamMembership.objects(device_id='v3-kid-dev', team_code=code).first() is None
    assert JoinRequest.objects(team_code=code, device_id='v3-kid-dev').count() == 1

    # The pending device is not yet a member.
    members = client.get(f'/teams/{code}', headers=cap).get_json()['members']
    assert all(m['member_id'] != _member_id('v3-kid-dev', code) for m in members)

    # Captain sees and accepts the request.
    reqs = client.get(f'/teams/{code}/requests', headers=cap).get_json()['requests']
    assert len(reqs) == 1
    accept = client.post(f'/teams/{code}/requests/{reqs[0]["member_id"]}',
                         json={'accept': True}, headers=cap)
    assert accept.status_code == 204
    assert DeviceTeamMembership.objects(
        device_id='v3-kid-dev', team_code=code, removed_at=None).first() is not None


def test_consent_gated_member_hidden_from_members_for_others(client, admin_headers, device_key):
    code = 'v3-consent-hide'
    Team(code=code, requires_consent=True).save()
    # Two accepted members, one without acknowledged consent.
    DeviceTeamMembership(device_id='v3-ch-a', team_code=code, member_id='mid-a',
                         role='captain').save()
    DeviceTeamMembership(device_id='v3-ch-b', team_code=code, member_id='mid-b').save()
    Player(device_id='v3-ch-a', name='Adult', consent_acknowledged_at=datetime.utcnow()).save()
    Player(device_id='v3-ch-b', name='Kid').save()  # no consent

    view = client.get(f'/teams/{code}', headers=admin_headers).get_json()
    member_ids = {m['member_id'] for m in view['members']}
    assert 'mid-a' in member_ids
    assert 'mid-b' not in member_ids  # hidden until consent acknowledged


# --- Acceptance #9 ---------------------------------------------------------

def test_lineup_if_match_conflict_returns_412(client, device_key):
    cap = _secret_headers(client, device_key, 'v3-l9-key', 'v3-l9-cap')
    code = 'v3-lineup-team'
    _join(client, cap, 'v3-l9-cap', code)
    mid = _member_id('v3-l9-cap', code)

    created = client.put(f'/teams/{code}/lineup', headers=cap,
                         json={'name': '4-4-2', 'slots': [{'member_id': mid, 'x': 0.5, 'y': 0.1}]})
    assert created.status_code == 204
    base = client.get(f'/teams/{code}/lineup', headers=cap).get_json()['updated_at']

    first = client.put(f'/teams/{code}/lineup', headers={**cap, 'If-Match': base},
                       json={'name': '4-3-3', 'slots': [{'name': 'Pat', 'x': 0.8, 'y': 0.5}]})
    assert first.status_code == 204

    second = client.put(f'/teams/{code}/lineup', headers={**cap, 'If-Match': base},
                        json={'name': '3-5-2', 'slots': [{'name': 'Sam', 'x': 0.2, 'y': 0.2}]})
    assert second.status_code == 412


def test_lineup_validation(client, device_key):
    cap = _secret_headers(client, device_key, 'v3-lv-key', 'v3-lv-cap')
    code = 'v3-lineupval-team'
    _join(client, cap, 'v3-lv-cap', code)
    mid = _member_id('v3-lv-cap', code)

    # both member_id and name set → invalid.
    bad_both = client.put(f'/teams/{code}/lineup', headers=cap,
                          json={'slots': [{'member_id': mid, 'name': 'Pat', 'x': 0.1, 'y': 0.1}]})
    assert bad_both.status_code == 400
    # x out of range → invalid.
    bad_coord = client.put(f'/teams/{code}/lineup', headers=cap,
                           json={'slots': [{'name': 'Pat', 'x': 1.5, 'y': 0.1}]})
    assert bad_coord.status_code == 400
    # > 11 slots → invalid.
    too_many = client.put(f'/teams/{code}/lineup', headers=cap,
                          json={'slots': [{'name': f'P{i}', 'x': 0.1, 'y': 0.1} for i in range(12)]})
    assert too_many.status_code == 400


def test_coach_can_write_lineup_player_cannot(client, device_key):
    cap = _secret_headers(client, device_key, 'v3-lc-cap-key', 'v3-lc-cap')
    coach = _secret_headers(client, device_key, 'v3-lc-coach-key', 'v3-lc-coach')
    player = _secret_headers(client, device_key, 'v3-lc-pl-key', 'v3-lc-pl')
    code = 'v3-lineuprole-team'
    _join(client, cap, 'v3-lc-cap', code)
    _join(client, coach, 'v3-lc-coach', code)
    _join(client, player, 'v3-lc-pl', code)

    # Coach role is self-assignable (V3 §1 matrix).
    coach_mid = _member_id('v3-lc-coach', code)
    assert client.put(f'/teams/{code}/members/{coach_mid}/role',
                      json={'role': 'coach'}, headers=coach).status_code == 204

    assert client.put(f'/teams/{code}/lineup', headers=coach,
                      json={'slots': [{'name': 'Pat', 'x': 0.5, 'y': 0.5}]}).status_code == 204
    denied = client.put(f'/teams/{code}/lineup', headers=player,
                        json={'slots': [{'name': 'Pat', 'x': 0.5, 'y': 0.5}]})
    assert denied.status_code == 403


# --- Acceptance #10 --------------------------------------------------------

def test_replays_are_idempotent(client, device_key):
    cap = _secret_headers(client, device_key, 'v3-r10-key', 'v3-r10-cap')
    code = 'v3-replay-team'
    assert _join(client, cap, 'v3-r10-cap', code).status_code == 201
    assert _join(client, cap, 'v3-r10-cap', code).status_code == 200  # rejoin no-op

    mid = _member_id('v3-r10-cap', code)
    body = {'name': '4-4-2', 'slots': [{'member_id': mid, 'x': 0.5, 'y': 0.1}]}
    assert client.put(f'/teams/{code}/lineup', json=body, headers=cap).status_code == 204
    assert client.put(f'/teams/{code}/lineup', json=body, headers=cap).status_code == 204
    assert TeamLineup.objects(team_code=code).count() == 1

    assert client.delete(f'/devices/v3-r10-cap/teams/{code}', headers=cap).status_code == 204
    assert client.delete(f'/devices/v3-r10-cap/teams/{code}', headers=cap).status_code == 204


# --- §3 code rotation ------------------------------------------------------

def test_code_rotation_grace_and_join_refusal(client, device_key):
    cap = _secret_headers(client, device_key, 'v3-rot-key', 'v3-rot-cap')
    old_code = 'v3-rotate-team'
    _join(client, cap, 'v3-rot-cap', old_code)

    rotated = client.post(f'/teams/{old_code}/code', headers=cap)
    assert rotated.status_code == 200
    new_code = rotated.get_json()['code']
    assert new_code and new_code != old_code

    # Existing members keep reading under both codes during the grace.
    assert client.get(f'/teams/{new_code}', headers=cap).status_code == 200
    assert client.get(f'/teams/{old_code}', headers=cap).status_code == 200

    # New joins under the old code are refused at once.
    newcomer = device_key('v3-rot-new-key', 'v3-rot-new')
    refused = _join(client, newcomer, 'v3-rot-new', old_code)
    assert refused.status_code == 409
    assert refused.get_json() == {'reason': 'code_rotated'}
    # New joins under the new code succeed.
    assert _join(client, newcomer, 'v3-rot-new', new_code).status_code == 201


def test_role_self_and_captain_only_matrix(client, device_key):
    cap = _secret_headers(client, device_key, 'v3-rm-cap-key', 'v3-rm-cap')
    player = _secret_headers(client, device_key, 'v3-rm-pl-key', 'v3-rm-pl')
    code = 'v3-rolematrix-team'
    _join(client, cap, 'v3-rm-cap', code)
    _join(client, player, 'v3-rm-pl', code)
    cap_mid = _member_id('v3-rm-cap', code)
    player_mid = _member_id('v3-rm-pl', code)

    # A player cannot set another member's role.
    assert client.put(f'/teams/{code}/members/{cap_mid}/role',
                      json={'role': 'coach'}, headers=player).status_code == 403
    # Setting role to captain is refused — use handover.
    assert client.put(f'/teams/{code}/members/{player_mid}/role',
                      json={'role': 'captain'}, headers=cap).status_code == 400
    # Captain hands over; old captain becomes a player.
    assert client.post(f'/teams/{code}/captain',
                       json={'member_id': player_mid}, headers=cap).status_code == 204
    assert DeviceTeamMembership.objects(
        device_id='v3-rm-pl', team_code=code).first().role == 'captain'
    assert DeviceTeamMembership.objects(
        device_id='v3-rm-cap', team_code=code).first().role == 'player'


# --- §4 next + §5 matches --------------------------------------------------

def test_next_round_trip_and_expiry(client, device_key):
    cap = _secret_headers(client, device_key, 'v3-next-key', 'v3-next-cap')
    code = 'v3-next-team'
    _join(client, cap, 'v3-next-cap', code)

    assert client.get(f'/teams/{code}/next', headers=cap).status_code == 404

    future = (datetime.utcnow() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
    put = client.put(f'/teams/{code}/next', headers=cap,
                     json={'kickoff': future, 'title': 'Rovers FC, home',
                           'place': 'Demo Park', 'in': [{'name': 'Pat'}]})
    assert put.status_code == 204
    got = client.get(f'/teams/{code}/next', headers=cap).get_json()
    assert got['title'] == 'Rovers FC, home'
    assert got['in'] == [{'name': 'Pat'}]

    # A kickoff more than 12h in the past is cleared (404).
    past = (datetime.utcnow() - timedelta(hours=13)).strftime('%Y-%m-%dT%H:%M:%SZ')
    client.put(f'/teams/{code}/next', headers={**cap, 'If-Match': got['updated_at']},
               json={'kickoff': past, 'title': 'Old'})
    assert client.get(f'/teams/{code}/next', headers=cap).status_code == 404


def test_team_matches_group_by_two_hour_window(client, device_key):
    cap = _secret_headers(client, device_key, 'v3-mm-cap-key', 'v3-mm-cap')
    other = _secret_headers(client, device_key, 'v3-mm-o-key', 'v3-mm-o')
    code = 'v3-matches-team'
    _join(client, cap, 'v3-mm-cap', code)
    _join(client, other, 'v3-mm-o', code)

    # Two uploads within 2h → one grouped match; a third a day later → separate.
    _upload(client, cap, 'v3-mm-cap', code, player_name='Cap',
            recorded_at='2026-07-13T09:00:00Z',
            stats={'total_distance_m': 9000.0, 'time_on_pitch_s': 5280.0, 'sprint_count': 14},
            events=[{'uuid': new_uuid(), 'kind': 'goalMine', 'date': '2026-07-13T09:20:00Z'}])
    _upload(client, other, 'v3-mm-o', code, player_name='Other',
            recorded_at='2026-07-13T09:30:00Z',
            stats={'total_distance_m': 8000.0, 'time_on_pitch_s': 5000.0, 'sprint_count': 9})
    _upload(client, cap, 'v3-mm-cap', code, player_name='Cap',
            recorded_at='2026-07-14T09:00:00Z',
            stats={'total_distance_m': 7000.0, 'time_on_pitch_s': 4800.0, 'sprint_count': 8})

    matches = client.get(f'/teams/{code}/matches', headers=cap).get_json()['matches']
    assert len(matches) == 2  # newest first
    assert matches[0]['kickoff'].startswith('2026-07-14')
    grouped = matches[1]
    assert grouped['kickoff'].startswith('2026-07-13')
    assert len(grouped['players']) == 2
    cap_row = next(p for p in grouped['players'] if p['player_name'] == 'Cap')
    assert cap_row['goals'] == 1
    assert cap_row['minutes'] == 88
    # member_id is exposed, device id is not.
    assert cap_row['member_id'] == _member_id('v3-mm-cap', code)


def test_team_matches_null_not_zero_for_unknown_stats(client, device_key):
    cap = _secret_headers(client, device_key, 'v3-mn-key', 'v3-mn-cap')
    code = 'v3-matchnull-team'
    _join(client, cap, 'v3-mn-cap', code)
    # Upload with no stats and no events → unknown fields are null, not 0.
    _upload(client, cap, 'v3-mn-cap', code, player_name='Cap')
    players = client.get(f'/teams/{code}/matches', headers=cap).get_json()['matches'][0]['players']
    row = players[0]
    assert row['distance_m'] is None
    assert row['sprints'] is None
    assert row['goals'] is None


# --- §6 invites ------------------------------------------------------------

def test_join_page_and_aasa(client):
    page = client.get('/join/ABC123?team=Red%20Dragons')
    assert page.status_code == 200
    assert 'text/html' in page.content_type
    text = page.get_data(as_text=True)
    assert 'ABC123' in text
    assert 'Red Dragons' in text

    aasa = client.get('/.well-known/apple-app-site-association')
    assert aasa.status_code == 200
    assert 'application/json' in aasa.content_type
    body = aasa.get_json()
    assert body['applinks']['details'][0]['components'] == [{'/': '/join/*'}]
    assert body['applinks']['details'][0]['appIDs'][0].endswith('com.nicemohawk.MatchTracker')


def test_join_rate_limit_per_device(app, client, device_key):
    app.config['JOIN_RATE_LIMIT_PER_DEVICE_PER_HOUR'] = 3
    headers = device_key('v3-rl-key', 'v3-rl-dev')
    codes = [f'v3-rl-team-{i}' for i in range(4)]
    statuses = [_join(client, headers, 'v3-rl-dev', code).status_code for code in codes]
    assert statuses[:3] == [201, 201, 201]
    assert statuses[3] == 429
