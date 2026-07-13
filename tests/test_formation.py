"""Tests for formation detection (V2 §3, formation.py).

The happy-path fixture synthesizes a 4-3-3: eleven devices, each with two
matches inside the window whose ``stats.mean_x`` / ``stats.mean_y`` cluster
(with small jitter) around a distinct 4-3-3 template slot. Three of the eleven
are stored 180°-rotated ``(1 − x, 1 − y)`` so the endpoint's mirror-alignment
must recover them before the fit succeeds.
"""

import random
from datetime import datetime, timedelta

from match_tracks.formation import FORMATION_TEMPLATES
from match_tracks.models import Match, Player

TEAM_CODE = 'formation-fixture'
JITTER_SEED = 7
MIRRORED_PLAYER_INDICES = {3, 5, 7}  # off-center slots that genuinely flip
INITIALS_PLAYER_INDEX = 1

READER_KEY = 'formation-reader-key'
READER_DEVICE = 'formation-reader-device'


def _make_match(device_id, mean_x, mean_y, recorded_at, uuid_suffix):
    """Persist a Match row carrying mean pitch coordinates in its stats."""
    Match(
        uuid=f'{device_id}-{uuid_suffix}',
        device_id=device_id,
        team_code=TEAM_CODE,
        recorded_at=recorded_at,
        stats={'mean_x': mean_x, 'mean_y': mean_y},
    ).save()


def _seed_four_three_three(app):
    """Create 11 players with jittered 2-match tracks around 4-3-3 slots."""
    template = FORMATION_TEMPLATES['4-3-3']
    rng = random.Random(JITTER_SEED)
    within_window = datetime.utcnow() - timedelta(days=1)
    device_ids = []

    for slot_index, slot in enumerate(template):
        device_id = f'formation-device-{slot_index:02d}'
        device_ids.append(device_id)

        if slot_index == INITIALS_PLAYER_INDEX:
            Player(device_id=device_id, name='Grace Hopper',
                   initials_only=True, team_code=TEAM_CODE).save()
        else:
            Player(device_id=device_id, name=f'Player {slot_index:02d}',
                   team_code=TEAM_CODE).save()

        for match_index in range(2):
            mean_x = slot['x'] + rng.uniform(-0.02, 0.02)
            mean_y = slot['y'] + rng.uniform(-0.02, 0.02)
            if slot_index in MIRRORED_PLAYER_INDICES:
                mean_x, mean_y = 1.0 - mean_x, 1.0 - mean_y
            _make_match(device_id, mean_x, mean_y, within_window, match_index)

    return device_ids


def _reader_headers(app, membership):
    """Register a member device key allowed to read the fixture team."""
    app.config['DEVICE_API_KEYS'][READER_KEY] = READER_DEVICE
    membership(READER_DEVICE, TEAM_CODE)
    return {'Authorization': f'APIKey {READER_KEY}'}


def test_formation_detects_four_three_three(app, client, membership):
    _seed_four_three_three(app)
    # A stale match (outside the window) for an otherwise-unseen device must be
    # ignored; if counted it would add a twelfth slot.
    _make_match('formation-stale-device', 0.5, 0.5,
                datetime.utcnow() - timedelta(days=400), 'stale')
    headers = _reader_headers(app, membership)

    response = client.get(f'/teams/{TEAM_CODE}/formation', headers=headers)

    assert response.status_code == 200
    body = response.get_json()
    assert body['name'] == '4-3-3'
    assert body['confidence'] > 0.6
    assert len(body['slots']) == 11

    roles = [slot['role'] for slot in body['slots']]
    assert 'goalkeeper' in roles

    player_names = [slot['player_name'] for slot in body['slots']]
    assert 'G. H.' in player_names  # initials_only player rendered as initials


def test_stale_matches_outside_window_are_ignored(app, client, membership):
    _seed_four_three_three(app)
    _make_match('formation-stale-device', 0.5, 0.5,
                datetime.utcnow() - timedelta(days=400), 'stale')
    headers = _reader_headers(app, membership)

    response = client.get(f'/teams/{TEAM_CODE}/formation', headers=headers)

    assert response.status_code == 200
    # The stale device did not become a twelfth slot.
    assert len(response.get_json()['slots']) == 11


def test_fewer_than_five_players_returns_insufficient_data(app, client, membership):
    within_window = datetime.utcnow() - timedelta(days=1)
    for index in range(4):
        device_id = f'sparse-device-{index}'
        _make_match(device_id, 0.2 + 0.1 * index, 0.5, within_window, '0')
    headers = _reader_headers(app, membership)

    response = client.get(f'/teams/{TEAM_CODE}/formation', headers=headers)

    assert response.status_code == 404
    assert response.get_json() == {'reason': 'insufficient_data'}


def test_matches_missing_mean_coordinates_are_not_counted(app, client, membership):
    within_window = datetime.utcnow() - timedelta(days=1)
    # Four fully-qualified devices...
    for index in range(4):
        _make_match(f'valid-device-{index}', 0.2 + 0.1 * index, 0.5,
                    within_window, '0')
    # ...plus a fifth whose only match lacks mean_x/mean_y: it must not count.
    Match(uuid='no-means-match', device_id='no-means-device',
          team_code=TEAM_CODE, recorded_at=within_window,
          stats={'total_distance_m': 1234.0}).save()
    headers = _reader_headers(app, membership)

    response = client.get(f'/teams/{TEAM_CODE}/formation', headers=headers)

    assert response.status_code == 404
    assert response.get_json() == {'reason': 'insufficient_data'}


def test_non_member_device_is_forbidden(app, client, device_key):
    _seed_four_three_three(app)
    headers = device_key('outsider-key', 'outsider-device')  # no membership

    response = client.get(f'/teams/{TEAM_CODE}/formation', headers=headers)

    assert response.status_code == 403
    assert response.get_json() == {'reason': 'not_a_member'}
