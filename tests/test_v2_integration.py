"""Coordinator integration tests for the V2 cross-cutting wiring:
multi-sport ingest (§4), idempotent replay (acceptance 8), event source
metadata (§10), seeded-field visibility (§8), membership validation on
session ingest (§6), and consent/initials enforcement in team stats (§5).
"""

from datetime import datetime, timedelta

from tests.helpers import (make_extended_session_payload, make_field_outline,
                           make_field_payload, make_match_track, make_event,
                           make_match_stats, new_uuid, register_device)

BASE_LATITUDE = 44.20
BASE_LONGITUDE = -84.55
VENUE_SPACING_DEGREES = 0.4


def _venue(index):
    return (BASE_LATITUDE + index * VENUE_SPACING_DEGREES,
            BASE_LONGITUDE + index * VENUE_SPACING_DEGREES)


# (Acceptance 4) A rugby-sized field is accepted with sport_id "rugby" but
# rejected as an implicit soccer observation, and nearby filters by sport.
def test_sport_id_gates_track_observations_and_filters_nearby(client, admin_headers, device_key):
    device_uuid = register_device(client, admin_headers)
    device_headers = device_key('v2-sport-device', device_uuid)

    rugby_latitude, rugby_longitude = _venue(1)
    # 140 x 69 m: valid rugby, implausible soccer (length > 130).
    rugby_track = make_match_track(rugby_latitude, rugby_longitude,
                                   length_m=140.0, width_m=69.0, num_points=400)

    soccer_attempt = make_extended_session_payload(new_uuid(), '2026-07-13T08:00:00Z',
                                                   rugby_track)
    assert client.post(f'/devices/{device_uuid}/sessions/',
                       json={'sessions': [soccer_attempt]},
                       headers=device_headers).status_code == 200
    nearby_as_soccer = client.get(
        f'/fields/nearby?lat={rugby_latitude}&lon={rugby_longitude}&radius_m=2000',
        headers=device_headers).get_json()['fields']
    assert nearby_as_soccer == []

    rugby_payload = make_extended_session_payload(new_uuid(), '2026-07-13T09:00:00Z',
                                                  rugby_track)
    rugby_payload['sport_id'] = 'rugby'
    assert client.post(f'/devices/{device_uuid}/sessions/',
                       json={'sessions': [rugby_payload]},
                       headers=device_headers).status_code == 200

    all_nearby = client.get(
        f'/fields/nearby?lat={rugby_latitude}&lon={rugby_longitude}&radius_m=2000',
        headers=device_headers).get_json()['fields']
    assert len(all_nearby) == 1

    rugby_filtered = client.get(
        f'/fields/nearby?lat={rugby_latitude}&lon={rugby_longitude}'
        '&radius_m=2000&sport_id=rugby',
        headers=device_headers).get_json()['fields']
    assert len(rugby_filtered) == 1

    soccer_filtered = client.get(
        f'/fields/nearby?lat={rugby_latitude}&lon={rugby_longitude}'
        '&radius_m=2000&sport_id=soccer',
        headers=device_headers).get_json()['fields']
    assert soccer_filtered == []


# (Acceptance 8) Replaying successful POSTs changes nothing and returns success.
def test_replaying_session_and_field_posts_changes_nothing(client, admin_headers, device_key):
    device_uuid = register_device(client, admin_headers)
    device_headers = device_key('v2-idempotency-device', device_uuid)
    latitude, longitude = _venue(2)

    field_uuid = new_uuid()
    field_payload = make_field_payload(field_uuid, '2026-07-13T07:00:00Z',
                                       make_field_outline(latitude, longitude))
    session_uuid = new_uuid()
    session_payload = make_extended_session_payload(
        session_uuid, '2026-07-13T08:00:00Z',
        make_match_track(latitude, longitude, num_points=10),
        duration_s=3000.0, team_code='v2-idem-team')

    for _ in range(2):
        assert client.post(f'/devices/{device_uuid}/fields/',
                           json={'fields': [field_payload]},
                           headers=device_headers).status_code == 200
    for _ in range(2):
        assert client.post(f'/devices/{device_uuid}/sessions/',
                           json={'sessions': [session_payload]},
                           headers=device_headers).status_code == 200

    # Canonical stores unchanged by the replays.
    matches_body = client.get(f'/devices/{device_uuid}/matches',
                              headers=device_headers).get_json()
    assert matches_body['total'] == 1

    nearby = client.get(f'/fields/nearby?lat={latitude}&lon={longitude}&radius_m=2000',
                        headers=device_headers).get_json()['fields']
    assert len(nearby) == 1
    assert nearby[0]['observation_count'] == 1  # replay did not double-count

    # Legacy embedded copies are not duplicated either (V2 tightening).
    embedded_sessions = client.get(f'/devices/{device_uuid}/sessions/').get_json()['sessions']
    assert len(embedded_sessions) == 1
    embedded_fields = client.get(f'/devices/{device_uuid}/fields/').get_json()['fields']
    assert len(embedded_fields) == 1


# (§10) Event source metadata is stored and echoed; aggregation counts both.
def test_event_source_metadata_round_trips_and_counts(client, admin_headers, device_key, membership):
    device_uuid = register_device(client, admin_headers)
    device_headers = device_key('v2-events-device', device_uuid)
    membership(device_uuid, 'v2-events-team')
    latitude, longitude = _venue(3)

    manual_goal = make_event('goalFor', '2026-07-13T09:10:00Z')
    automatic_goal = make_event('goalMine', '2026-07-13T09:20:00Z')
    automatic_goal['source'] = 'automatic'
    flagged = make_event('flag', '2026-07-13T09:30:00Z')
    flagged['source'] = 'manual'

    session_uuid = new_uuid()
    payload = make_extended_session_payload(
        session_uuid, '2026-07-13T09:00:00Z',
        make_match_track(latitude, longitude, num_points=10),
        team_code='v2-events-team', player_name='Event Tester',
        events=[manual_goal, automatic_goal, flagged],
        stats=make_match_stats(time_on_pitch_s=600.0))
    assert client.post(f'/devices/{device_uuid}/sessions/',
                       json={'sessions': [payload]},
                       headers=device_headers).status_code == 200

    detail = client.get(f'/devices/{device_uuid}/matches/{session_uuid}',
                        headers=device_headers).get_json()['match']
    sources = {event['uuid']: event.get('source') for event in detail['events']}
    assert sources[automatic_goal['uuid']] == 'automatic'
    assert sources[flagged['uuid']] == 'manual'
    assert sources[manual_goal['uuid']] is None  # absent means manual

    stats = client.get('/teams/v2-events-team/stats', headers=admin_headers).get_json()
    assert stats['players'][0]['goals'] == 2  # manual + automatic both count


# (§8) Seeded fields stay hidden above min_confidence 0.25 until confirmed.
def test_seeded_fields_gate_on_confidence_until_confirmed(client, admin_headers, device_key, app):
    from match_tracks.models import CommunityField

    device_uuid = register_device(client, admin_headers)
    device_headers = device_key('v2-seeded-device', device_uuid)
    latitude, longitude = _venue(4)

    CommunityField(
        uuid=new_uuid(), source='community', confidence=0.25, observation_count=0,
        seeded=True, has_trained_observation=False, outline=None,
        rect_center_lat=latitude, rect_center_lon=longitude,
        rect_length_m=100.0, rect_width_m=64.0, rect_heading_deg=0.0,
        contributing_device_ids=[],
    ).save()

    low_bar = client.get(
        f'/fields/nearby?lat={latitude}&lon={longitude}&radius_m=2000&min_confidence=0.2',
        headers=device_headers).get_json()['fields']
    assert len(low_bar) == 1

    high_bar = client.get(
        f'/fields/nearby?lat={latitude}&lon={longitude}&radius_m=2000&min_confidence=0.5',
        headers=device_headers).get_json()['fields']
    assert high_bar == []

    # A real observation confirms the seeded field and lifts it out of the gate.
    confirming = make_extended_session_payload(
        new_uuid(), '2026-07-13T10:00:00Z',
        make_match_track(latitude, longitude, num_points=400))
    assert client.post(f'/devices/{device_uuid}/sessions/',
                       json={'sessions': [confirming]},
                       headers=device_headers).status_code == 200

    confirmed = client.get(
        f'/fields/nearby?lat={latitude}&lon={longitude}&radius_m=2000&min_confidence=0.29',
        headers=device_headers).get_json()['fields']
    assert len(confirmed) == 1
    assert confirmed[0]['observation_count'] == 1


# (§6) Session ingest validates team membership; solo devices auto-join.
def test_session_ingest_validates_team_membership(client, admin_headers, device_key, membership):
    latitude, longitude = _venue(5)

    solo_device = register_device(client, admin_headers)
    solo_headers = device_key('v2-solo-device', solo_device)
    solo_payload = make_extended_session_payload(
        new_uuid(), '2026-07-13T08:00:00Z',
        make_match_track(latitude, longitude, num_points=10),
        team_code='v2-solo-team')
    # No memberships anywhere → auto-join on first tagged upload.
    assert client.post(f'/devices/{solo_device}/sessions/',
                       json={'sessions': [solo_payload]},
                       headers=solo_headers).status_code == 200
    teams = client.get(f'/devices/{solo_device}/teams', headers=solo_headers).get_json()
    assert [team['team_code'] for team in teams['teams']] == ['v2-solo-team']

    member_device = register_device(client, admin_headers)
    member_headers = device_key('v2-member-device', member_device)
    membership(member_device, 'v2-home-team')
    foreign_payload = make_extended_session_payload(
        new_uuid(), '2026-07-13T09:00:00Z',
        make_match_track(latitude, longitude, num_points=10),
        team_code='v2-other-team')
    response = client.post(f'/devices/{member_device}/sessions/',
                           json={'sessions': [foreign_payload]},
                           headers=member_headers)
    assert response.status_code == 400
    assert response.get_json() == {'reason': 'not_a_member'}


# (§5) Team stats honor initials_only and consent gating.
def test_team_stats_respect_initials_and_consent(client, admin_headers, device_key, membership):
    from match_tracks.models import Player, Team

    latitude, longitude = _venue(6)
    team_code = 'v2-minors-team'
    assert client.post('/teams', json={'code': team_code, 'requires_consent': True},
                       headers=admin_headers).status_code == 201

    consented_device = register_device(client, admin_headers)
    consented_headers = device_key('v2-consented-device', consented_device)
    membership(consented_device, team_code)
    unconsented_device = register_device(client, admin_headers)
    unconsented_headers = device_key('v2-unconsented-device', unconsented_device)
    membership(unconsented_device, team_code)

    for device_uuid, headers, player_name in (
        (consented_device, consented_headers, 'Casey Consented'),
        (unconsented_device, unconsented_headers, 'Pat Pending'),
    ):
        payload = make_extended_session_payload(
            new_uuid(), '2026-07-13T09:00:00Z',
            make_match_track(latitude, longitude, num_points=10),
            team_code=team_code, player_name=player_name,
            stats=make_match_stats(time_on_pitch_s=600.0))
        assert client.post(f'/devices/{device_uuid}/sessions/',
                           json={'sessions': [payload]},
                           headers=headers).status_code == 200

    Player.objects(device_id=consented_device).update_one(set__initials_only=True)
    assert client.post(f'/devices/{consented_device}/consent',
                       json={'guardian_name': 'A Guardian', 'acknowledged': True},
                       headers=consented_headers).status_code == 200

    stats = client.get(f'/teams/{team_code}/stats', headers=admin_headers).get_json()
    names = [player['player_name'] for player in stats['players']]
    assert names == ['C. C.']  # initials rendered; unconsented player hidden
