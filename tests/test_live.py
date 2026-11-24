"""Acceptance tests for V2 §1 live telemetry (match_tracks/live.py)."""

from datetime import datetime, timedelta, timezone

from match_tracks.models import LiveStatus, Player, Team


def _live_body(**overrides):
    body = {
        'match_uuid': 'match-uuid-0001',
        'team_code': 'LIVE-A',
        'sequence': 1,
        'elapsed_s': 600.0,
        'heart_rate': 150.0,
        'distance_m': 1200.0,
        'current_speed': 4.5,
        'on_pitch': True,
        'us_goals': 1,
        'them_goals': 0,
        'x': 0.4,
        'y': 0.6,
    }
    body.update(overrides)
    return body


def test_accepted_post_is_204_and_appears_in_team_live(app, client, device_key, membership):
    team_code = 'LIVE-ACCEPT'
    device_headers = device_key('key-accept', 'dev-accept')
    membership('dev-accept', team_code)

    response = client.post('/devices/dev-accept/live', headers=device_headers,
                           json=_live_body(team_code=team_code))
    assert response.status_code == 204
    assert response.get_data() == b''

    listing = client.get(f'/teams/{team_code}/live', headers=device_headers)
    assert listing.status_code == 200
    players = listing.get_json()['players']
    assert len(players) == 1
    entry = players[0]
    assert entry['x'] == 0.4
    assert entry['y'] == 0.6
    assert entry['heart_rate'] == 150.0
    assert entry['distance_m'] == 1200.0
    assert entry['on_pitch'] is True
    assert entry['stale'] is False


def test_sequence_replay_and_regression_are_stale_and_do_not_write(app, client, device_key,
                                                                   membership):
    team_code = 'LIVE-SEQ'
    device_headers = device_key('key-seq', 'dev-seq')
    membership('dev-seq', team_code)

    first = client.post('/devices/dev-seq/live', headers=device_headers,
                        json=_live_body(team_code=team_code, sequence=5, distance_m=100.0))
    assert first.status_code == 204

    replay = client.post('/devices/dev-seq/live', headers=device_headers,
                         json=_live_body(team_code=team_code, sequence=5, distance_m=999.0))
    assert replay.status_code == 200
    assert replay.get_json() == {'stale': True}

    regression = client.post('/devices/dev-seq/live', headers=device_headers,
                             json=_live_body(team_code=team_code, sequence=3, distance_m=888.0))
    assert regression.status_code == 200
    assert regression.get_json() == {'stale': True}

    stored = LiveStatus.objects(device_id='dev-seq').first()
    assert stored.sequence == 5
    assert stored.distance_m == 100.0


def test_different_match_uuid_with_lower_sequence_overwrites(app, client, device_key,
                                                             membership):
    team_code = 'LIVE-NEWMATCH'
    device_headers = device_key('key-newmatch', 'dev-newmatch')
    membership('dev-newmatch', team_code)

    first = client.post('/devices/dev-newmatch/live', headers=device_headers,
                        json=_live_body(team_code=team_code, match_uuid='match-a', sequence=5))
    assert first.status_code == 204

    second = client.post('/devices/dev-newmatch/live', headers=device_headers,
                         json=_live_body(team_code=team_code, match_uuid='match-b', sequence=1))
    assert second.status_code == 204

    stored = LiveStatus.objects(device_id='dev-newmatch').first()
    assert stored.match_uuid == 'match-b'
    assert stored.sequence == 1


def test_stale_flag_reflects_snapshot_age(app, client, admin_headers):
    team_code = 'LIVE-STALE'
    old_when = datetime.now(timezone.utc) - timedelta(seconds=31)
    LiveStatus(device_id='dev-old', team_code=team_code, match_uuid='m-old',
               sequence=1, updated_at=old_when, x=0.1, y=0.2).save()
    LiveStatus(device_id='dev-fresh', team_code=team_code, match_uuid='m-fresh',
               sequence=1, updated_at=datetime.now(timezone.utc), x=0.3, y=0.4).save()

    listing = client.get(f'/teams/{team_code}/live', headers=admin_headers)
    assert listing.status_code == 200
    stale_by_x = {entry['x']: entry['stale'] for entry in listing.get_json()['players']}
    assert stale_by_x[0.1] is True
    assert stale_by_x[0.3] is False


def test_initials_only_player_name_is_rendered(app, client, admin_headers):
    team_code = 'LIVE-INITIALS'
    Player(device_id='dev-initials', name='Ben Lachman', initials_only=True,
           team_code=team_code).save()
    LiveStatus(device_id='dev-initials', team_code=team_code, match_uuid='m1',
               sequence=1, updated_at=datetime.now(timezone.utc)).save()

    listing = client.get(f'/teams/{team_code}/live', headers=admin_headers)
    players = listing.get_json()['players']
    assert len(players) == 1
    assert players[0]['player_name'] == 'B. L.'


def test_consent_gated_player_is_excluded(app, client, admin_headers):
    team_code = 'LIVE-CONSENT'
    Team(code=team_code, requires_consent=True).save()
    Player(device_id='dev-noconsent', name='Kid Player', team_code=team_code).save()
    LiveStatus(device_id='dev-noconsent', team_code=team_code, match_uuid='m1',
               sequence=1, updated_at=datetime.now(timezone.utc)).save()

    listing = client.get(f'/teams/{team_code}/live', headers=admin_headers)
    assert listing.status_code == 200
    assert listing.get_json()['players'] == []


def test_non_member_device_cannot_post_live(app, client, device_key):
    device_headers = device_key('key-outsider', 'dev-outsider')
    response = client.post('/devices/dev-outsider/live', headers=device_headers,
                           json=_live_body(team_code='LIVE-NOTMINE'))
    assert response.status_code == 403
    assert response.get_json() == {'reason': 'not_a_member'}


def test_device_cannot_post_for_a_different_device(app, client, device_key, membership):
    team_code = 'LIVE-WRONGDEV'
    device_headers = device_key('key-devx', 'dev-x')
    membership('dev-x', team_code)
    membership('dev-y', team_code)

    response = client.post('/devices/dev-y/live', headers=device_headers,
                           json=_live_body(team_code=team_code))
    assert response.status_code == 403


def test_entitlement_gate_on_post_only(app, client, device_key, membership,
                                       grant_entitlement):
    app.config['ENTITLEMENTS_ENFORCED'] = True
    team_code = 'LIVE-ENTITLE'
    device_headers = device_key('key-entitle', 'dev-entitle')
    membership('dev-entitle', team_code)

    without = client.post('/devices/dev-entitle/live', headers=device_headers,
                          json=_live_body(team_code=team_code))
    assert without.status_code == 402
    assert without.get_json() == {'reason': 'entitlement_required'}

    # Reads never require an entitlement.
    read_without = client.get(f'/teams/{team_code}/live', headers=device_headers)
    assert read_without.status_code == 200

    grant_entitlement('dev-entitle', datetime.now(timezone.utc) + timedelta(days=30))
    with_entitlement = client.post('/devices/dev-entitle/live', headers=device_headers,
                                   json=_live_body(team_code=team_code))
    assert with_entitlement.status_code == 204

    read_with = client.get(f'/teams/{team_code}/live', headers=device_headers)
    assert read_with.status_code == 200


def test_live_rate_limit_returns_429(app, client, device_key, membership):
    app.config['RATE_LIMIT_LIVE_PER_MINUTE'] = 2
    team_code = 'LIVE-RATE'
    device_headers = device_key('key-rate', 'dev-rate')
    membership('dev-rate', team_code)

    first = client.post('/devices/dev-rate/live', headers=device_headers,
                        json=_live_body(team_code=team_code, sequence=1))
    second = client.post('/devices/dev-rate/live', headers=device_headers,
                         json=_live_body(team_code=team_code, sequence=2))
    third = client.post('/devices/dev-rate/live', headers=device_headers,
                        json=_live_body(team_code=team_code, sequence=3))

    assert first.status_code == 204
    assert second.status_code == 204
    assert third.status_code == 429
