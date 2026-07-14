"""Concurrency-safety of the session-ingest upserts.

The client's upload queue fires several POSTs for the same device at once, so
the shared-row upserts (Player by device_id, Team by code, Match by uuid) race:
two requests both see "no row", both insert, and the second hits the unique
index with a DuplicateKeyError -> 500. A real race needs real MongoDB
concurrency, which mongomock can't reproduce, so these tests force the
NotUniqueError deterministically (a concurrent winner inserts the row between
our fetch and our save) and assert each helper recovers instead of 500'ing.
"""

from mongoengine import NotUniqueError

from match_tracks import routes
from match_tracks.models import Player, Team, Match


def test_upsert_player_survives_concurrent_insert(app, monkeypatch):
    device_id = 'race-player-device'
    real_save = Player.save
    state = {'first': True}

    def flaky_save(self, *args, **kwargs):
        if state['first']:
            state['first'] = False
            real_save(Player(device_id=device_id, name='Winner'))  # concurrent winner
            raise NotUniqueError('dup device_id')
        return real_save(self, *args, **kwargs)

    monkeypatch.setattr(Player, 'save', flaky_save)
    routes._upsert_player(device_id, 'Loser', 'TEAM')  # must not raise
    monkeypatch.setattr(Player, 'save', real_save)

    rows = Player.objects(device_id=device_id)
    assert rows.count() == 1                 # no duplicate row
    assert rows.first().name == 'Loser'      # last-write-wins applied over the winner
    assert rows.first().team_code == 'TEAM'


def test_save_match_survives_concurrent_insert(app, monkeypatch):
    match_uuid = 'race-match-uuid'
    real_save = Match.save
    state = {'first': True}

    def flaky_save(self, *args, **kwargs):
        if state['first']:
            state['first'] = False
            real_save(Match(uuid=match_uuid, device_id='d', team_code='OLD'))
            raise NotUniqueError('dup uuid')
        return real_save(self, *args, **kwargs)

    monkeypatch.setattr(Match, 'save', flaky_save)
    routes._save_match_record(match_uuid, None,
                              {'device_id': 'd', 'team_code': 'NEW'})  # must not raise
    monkeypatch.setattr(Match, 'save', real_save)

    rows = Match.objects(uuid=match_uuid)
    assert rows.count() == 1
    assert rows.first().team_code == 'NEW'   # our values applied over the winner


def test_ensure_team_tolerates_concurrent_create(app, monkeypatch):
    code = 'RACE-TEAM'
    real_save = Team.save

    def flaky_save(self, *args, **kwargs):
        real_save(Team(code=code, name=None))  # concurrent winner
        raise NotUniqueError('dup code')

    monkeypatch.setattr(Team, 'save', flaky_save)
    routes._ensure_team(code)  # must not raise
    monkeypatch.setattr(Team, 'save', real_save)

    assert Team.objects(code=code).count() == 1


def test_ingest_batch_with_shared_player_and_team_is_idempotent(client, admin_headers):
    """Sequential sanity: many sessions for one device sharing a player/team
    upsert cleanly to a single Player/Team and one Match each."""
    from tests.helpers import (make_extended_session_payload, make_match_track,
                               new_uuid, register_device)
    device_uuid = register_device(client, admin_headers)
    for _ in range(5):
        payload = make_extended_session_payload(
            new_uuid(), '2026-07-14T02:00:00Z',
            make_match_track(44.10, -84.10, num_points=10),
            team_code='SHARED', player_name='Sharer')
        assert client.post(f'/devices/{device_uuid}/sessions/',
                           json={'sessions': [payload]},
                           headers=admin_headers).status_code == 200
    assert Player.objects(device_id=device_uuid).count() == 1
    assert Team.objects(code='SHARED').count() == 1
    assert Match.objects(device_id=device_uuid).count() == 5
