"""Acceptance tests for V2 §11 — the team event timeline (match_tracks/events.py).

Covers the live-log ingest on /devices/<id>/live (new_events), the unified
GET /teams/<code>/events feed (dedup, ordering, initials, since_hours,
membership), the coach annotation endpoint, and reconciliation of uploaded
session events over provisional live-log copies (coach_label preserved).
"""

from datetime import datetime, timedelta

from match_tracks.models import MatchEvent, Player


def _iso_z(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%SZ')


def _recent(minutes_ago=1):
    return _iso_z(datetime.utcnow() - timedelta(minutes=minutes_ago))


def _event(event_id, kind, date, note=None, source=None):
    event = {'id': event_id, 'kind': kind, 'date': date}
    if note is not None:
        event['note'] = note
    if source is not None:
        event['source'] = source
    return event


def _live_body(team_code, match_uuid='match-evt-1', sequence=1, new_events=None):
    body = {'match_uuid': match_uuid, 'team_code': team_code, 'sequence': sequence,
            'elapsed_s': 60.0, 'distance_m': 100.0, 'on_pitch': True}
    if new_events is not None:
        body['new_events'] = new_events
    return body


def test_live_new_events_surface_in_team_timeline_oldest_first(client, device_key, membership):
    team = 'EVT-TIMELINE'
    headers = device_key('k-evt1', 'dev-evt1')
    membership('dev-evt1', team)
    Player(device_id='dev-evt1', name='Ben L').save()

    resp = client.post('/devices/dev-evt1/live', headers=headers, json=_live_body(
        team, new_events=[
            _event('e-late', 'flag', _recent(1)),
            _event('e-early', 'goalMine', _recent(5), note='top corner', source='manual'),
        ]))
    assert resp.status_code == 204

    feed = client.get(f'/teams/{team}/events', headers=headers).get_json()['events']
    assert [e['id'] for e in feed] == ['e-early', 'e-late']  # ascending by date
    early = feed[0]
    assert early['player_name'] == 'Ben L'
    assert early['kind'] == 'goalMine'
    assert early['match_uuid'] == 'match-evt-1'
    assert early['note'] == 'top corner'
    assert early['source'] == 'manual'
    assert early['date'].endswith('Z')
    assert 'coach_label' not in early  # no annotation yet


def test_live_resend_updates_event_latest_wins(client, device_key, membership):
    team = 'EVT-MUT'
    headers = device_key('k-evt2', 'dev-evt2')
    membership('dev-evt2', team)

    # Streamed as goalFor; the watch then corrects it to goalMine ("who scored?")
    # and re-sends with the SAME id + a newer sequence. Latest wins (upsert).
    client.post('/devices/dev-evt2/live', headers=headers, json=_live_body(
        team, sequence=1, new_events=[_event('e-mut', 'goalFor', _recent(2), note='first')]))
    client.post('/devices/dev-evt2/live', headers=headers, json=_live_body(
        team, sequence=2, new_events=[_event('e-mut', 'goalMine', _recent(1), note='SECOND')]))

    assert MatchEvent.objects(event_uuid='e-mut').count() == 1  # no duplicate
    feed = client.get(f'/teams/{team}/events', headers=headers).get_json()['events']
    assert len(feed) == 1
    assert feed[0]['kind'] == 'goalMine'   # corrected kind wins
    assert feed[0]['note'] == 'SECOND'


def test_stale_live_post_does_not_append_new_events(client, device_key, membership):
    team = 'EVT-STALE'
    headers = device_key('k-evt3', 'dev-evt3')
    membership('dev-evt3', team)

    client.post('/devices/dev-evt3/live', headers=headers,
                json=_live_body(team, sequence=5, new_events=[_event('e-ok', 'flag', _recent(1))]))
    # A stale (older-sequence) POST for the same match is rejected; its events must not append.
    stale = client.post('/devices/dev-evt3/live', headers=headers,
                        json=_live_body(team, sequence=2,
                                        new_events=[_event('e-stale', 'flag', _recent(1))]))
    assert stale.status_code == 200
    assert stale.get_json() == {'stale': True}
    assert MatchEvent.objects(event_uuid='e-stale').first() is None
    feed = client.get(f'/teams/{team}/events', headers=headers).get_json()['events']
    assert [e['id'] for e in feed] == ['e-ok']


def test_timeline_respects_initials_only_and_since_hours(client, device_key, membership):
    team = 'EVT-PRIV'
    headers = device_key('k-evt4', 'dev-evt4')
    membership('dev-evt4', team)
    Player(device_id='dev-evt4', name='Ben Lachman', initials_only=True).save()

    client.post('/devices/dev-evt4/live', headers=headers, json=_live_body(
        team, new_events=[
            _event('e-inwindow', 'flag', _recent(30)),
            _event('e-old', 'flag', _iso_z(datetime.utcnow() - timedelta(hours=10))),
        ]))

    feed = client.get(f'/teams/{team}/events?since_hours=6', headers=headers).get_json()['events']
    assert [e['id'] for e in feed] == ['e-inwindow']  # 10h-old event outside the window
    assert feed[0]['player_name'] == 'B. L.'  # initials_only applied

    wide = client.get(f'/teams/{team}/events?since_hours=24', headers=headers).get_json()['events']
    assert {e['id'] for e in wide} == {'e-inwindow', 'e-old'}


def test_timeline_requires_membership(client, device_key, membership):
    team = 'EVT-MEMBER'
    owner = device_key('k-evt5', 'dev-evt5')
    membership('dev-evt5', team)
    client.post('/devices/dev-evt5/live', headers=owner,
                json=_live_body(team, new_events=[_event('e-m', 'flag', _recent(1))]))

    outsider = device_key('k-evt5x', 'dev-evt5x')  # not a member of EVT-MEMBER
    resp = client.get(f'/teams/{team}/events', headers=outsider)
    assert resp.status_code == 403


def test_annotation_sets_coach_label_separate_from_note_idempotent_and_clearable(
        client, device_key, membership):
    team = 'EVT-ANNO'
    headers = device_key('k-evt6', 'dev-evt6')
    membership('dev-evt6', team)
    client.post('/devices/dev-evt6/live', headers=headers, json=_live_body(
        team, new_events=[_event('e-anno', 'goalMine', _recent(1), note='player note')]))

    # Set a coach label — the player's note is untouched.
    r1 = client.post('/matches/match-evt-1/events/e-anno/annotation', headers=headers,
                     json={'label': 'great finish'})
    assert r1.status_code == 204
    event = client.get(f'/teams/{team}/events', headers=headers).get_json()['events'][0]
    assert event['coach_label'] == 'great finish'
    assert event['note'] == 'player note'

    # Idempotent overwrite.
    client.post('/matches/match-evt-1/events/e-anno/annotation', headers=headers,
                json={'label': 'clinical'})
    event = client.get(f'/teams/{team}/events', headers=headers).get_json()['events'][0]
    assert event['coach_label'] == 'clinical'

    # Empty label clears it.
    client.post('/matches/match-evt-1/events/e-anno/annotation', headers=headers,
                json={'label': ''})
    event = client.get(f'/teams/{team}/events', headers=headers).get_json()['events'][0]
    assert 'coach_label' not in event
    assert event['note'] == 'player note'


def test_annotation_resolves_by_event_id_regardless_of_match_uuid(client, device_key, membership):
    team = 'EVT-404'
    headers = device_key('k-evt7', 'dev-evt7')
    membership('dev-evt7', team)
    client.post('/devices/dev-evt7/live', headers=headers,
                json=_live_body(team, new_events=[_event('e-known', 'flag', _recent(1))]))

    # A DIFFERENT match uuid in the path still resolves the event by its id -> 204
    # (the live match_uuid is unreliable, so annotation keys by event id alone).
    ok = client.post('/matches/some-other-live-uuid/events/e-known/annotation',
                     headers=headers, json={'label': 'x'})
    assert ok.status_code == 204
    # A truly unknown event id -> 404.
    unknown = client.post('/matches/match-evt-1/events/does-not-exist/annotation',
                          headers=headers, json={'label': 'x'})
    assert unknown.status_code == 404


def test_annotation_requires_membership_and_entitlement(client, app, device_key, membership,
                                                        grant_entitlement):
    team = 'EVT-ENT'
    author = device_key('k-evt8', 'dev-evt8')
    membership('dev-evt8', team)
    client.post('/devices/dev-evt8/live', headers=author,
                json=_live_body(team, new_events=[_event('e-ent', 'flag', _recent(1))]))

    # Non-member of the event's team -> 403.
    outsider = device_key('k-evt8x', 'dev-evt8x')
    assert client.post('/matches/match-evt-1/events/e-ent/annotation', headers=outsider,
                       json={'label': 'x'}).status_code == 403

    # Member but no active entitlement, with enforcement on -> 402.
    app.config['ENTITLEMENTS_ENFORCED'] = True
    try:
        coach = device_key('k-evt8c', 'dev-evt8c')
        membership('dev-evt8c', team)
        assert client.post('/matches/match-evt-1/events/e-ent/annotation', headers=coach,
                           json={'label': 'x'}).status_code == 402

        grant_entitlement('dev-evt8c', datetime.utcnow() + timedelta(days=30))
        assert client.post('/matches/match-evt-1/events/e-ent/annotation', headers=coach,
                           json={'label': 'now allowed'}).status_code == 204
    finally:
        app.config['ENTITLEMENTS_ENFORCED'] = False


def test_session_upload_reconciles_across_unreliable_live_match_uuid(client, device_key, membership):
    team = 'EVT-RECON'
    headers = device_key('k-evt9', 'dev-evt9')
    membership('dev-evt9', team)

    # Live: provisional event under the per-launch live match id (NOT the session uuid).
    client.post('/devices/dev-evt9/live', headers=headers, json=_live_body(
        team, match_uuid='live-launch-xyz',
        new_events=[_event('e-recon', 'goalMine', _recent(3), note='rough note', source='automatic')]))
    # Coach annotates it via the live match id (the only id the client has mid-match).
    assert client.post('/matches/live-launch-xyz/events/e-recon/annotation', headers=headers,
                       json={'label': 'keep me'}).status_code == 204

    # Upload the finished match under the REAL session uuid, editing note + source.
    upload = client.post('/devices/dev-evt9/sessions', headers=headers, json={'sessions': [{
        'uuid': 'm-real', 'recorded_at': _recent(4), 'duration_s': 600.0, 'team_code': team,
        'player_name': 'Ben L', 'track': {'coordinates': [[1, 2]]},
        'events': [_event('e-recon', 'goalMine', _recent(3), note='edited note', source='manual')],
    }]})
    assert upload.status_code == 200

    assert MatchEvent.objects(event_uuid='e-recon').count() == 1  # no duplicate
    row = MatchEvent.objects(event_uuid='e-recon').first()
    assert row.provisional is False                 # reconciled
    assert row.match_uuid == 'm-real'               # now the real session uuid
    event = client.get(f'/teams/{team}/events', headers=headers).get_json()['events'][0]
    assert event['note'] == 'edited note'           # uploaded copy wins
    assert event['source'] == 'manual'
    assert event['coach_label'] == 'keep me'        # survived reconciliation across the uuid change
    assert event['player_name'] == 'Ben L'


def test_session_upload_drops_undone_live_only_events(client, device_key, membership):
    team = 'EVT-UNDO'
    headers = device_key('k-evt10', 'dev-evt10')
    membership('dev-evt10', team)

    # Three live events: two in this match's window, one from much earlier (another match).
    client.post('/devices/dev-evt10/live', headers=headers, json=_live_body(
        team, match_uuid='live-launch-2', new_events=[
            _event('e-keep', 'goalMine', _recent(5)),
            _event('e-undone', 'flag', _recent(4)),
            _event('e-other', 'flag', _iso_z(datetime.utcnow() - timedelta(minutes=120))),
        ]))

    # The player undid e-undone; the authoritative upload omits it.
    upload = client.post('/devices/dev-evt10/sessions', headers=headers, json={'sessions': [{
        'uuid': 'm-undo', 'recorded_at': _recent(6), 'duration_s': 600.0, 'team_code': team,
        'track': {'coordinates': [[1, 2]]},
        'events': [_event('e-keep', 'goalMine', _recent(5))],
    }]})
    assert upload.status_code == 200

    ids = {e['id'] for e in client.get(f'/teams/{team}/events', headers=headers).get_json()['events']}
    assert 'e-keep' in ids                              # kept + reconciled
    assert 'e-undone' not in ids                        # dropped — undone, absent from upload
    assert MatchEvent.objects(event_uuid='e-undone').first() is None
    assert 'e-other' in ids                             # outside the window, untouched
