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


# --- Wave 4 review-fix regressions -------------------------------------------

def test_team_less_match_comments_are_owner_only(client, admin_headers, device_key):
    from match_tracks.models import Match

    owner_device = register_device(client, admin_headers)
    owner_headers = device_key('v2-fix-owner', owner_device)
    stranger_device = register_device(client, admin_headers)
    stranger_headers = device_key('v2-fix-stranger', stranger_device)

    match_uuid = new_uuid()
    Match(uuid=match_uuid, device_id=owner_device, team_code=None).save()

    payload = {'id': new_uuid(), 'body': 'private note'}
    assert client.post(f'/matches/{match_uuid}/comments', json=payload,
                       headers=owner_headers).status_code == 201
    assert client.get(f'/matches/{match_uuid}/comments',
                      headers=owner_headers).status_code == 200

    stranger_post = client.post(f'/matches/{match_uuid}/comments',
                                json={'id': new_uuid(), 'body': 'intrusion'},
                                headers=stranger_headers)
    assert stranger_post.status_code == 403
    stranger_get = client.get(f'/matches/{match_uuid}/comments',
                              headers=stranger_headers)
    assert stranger_get.status_code == 403

    assert client.get(f'/matches/{match_uuid}/comments',
                      headers=admin_headers).status_code == 200


def test_comment_limit_zero_and_negative_are_clamped(client, admin_headers, device_key):
    from match_tracks.models import Match

    device_uuid = register_device(client, admin_headers)
    device_headers = device_key('v2-fix-limit', device_uuid)
    match_uuid = new_uuid()
    Match(uuid=match_uuid, device_id=device_uuid, team_code=None).save()

    for index in range(3):
        assert client.post(f'/matches/{match_uuid}/comments',
                           json={'id': new_uuid(), 'body': f'comment {index}'},
                           headers=device_headers).status_code == 201

    for bad_limit in (0, -5):
        body = client.get(f'/matches/{match_uuid}/comments?limit={bad_limit}',
                          headers=device_headers).get_json()
        assert len(body['comments']) == 1  # clamped to 1, never "no limit"


def test_non_string_comment_body_is_rejected(client, admin_headers, device_key):
    from match_tracks.models import Match

    device_uuid = register_device(client, admin_headers)
    device_headers = device_key('v2-fix-body-type', device_uuid)
    match_uuid = new_uuid()
    Match(uuid=match_uuid, device_id=device_uuid, team_code=None).save()

    response = client.post(f'/matches/{match_uuid}/comments',
                           json={'id': new_uuid(), 'body': [1, 2, 3]},
                           headers=device_headers)
    assert response.status_code == 400


def test_match_reads_are_scoped_to_the_device_or_admin(client, admin_headers, device_key):
    latitude, longitude = _venue(7)
    owner_device = register_device(client, admin_headers)
    owner_headers = device_key('v2-fix-matches-owner', owner_device)
    other_device = register_device(client, admin_headers)
    other_headers = device_key('v2-fix-matches-other', other_device)

    session_uuid = new_uuid()
    payload = make_extended_session_payload(
        session_uuid, '2026-07-13T09:00:00Z',
        make_match_track(latitude, longitude, num_points=10))
    assert client.post(f'/devices/{owner_device}/sessions/',
                       json={'sessions': [payload]},
                       headers=owner_headers).status_code == 200

    assert client.get(f'/devices/{owner_device}/matches',
                      headers=owner_headers).status_code == 200
    assert client.get(f'/devices/{owner_device}/matches',
                      headers=other_headers).status_code == 403
    assert client.get(f'/devices/{owner_device}/matches/{session_uuid}',
                      headers=other_headers).status_code == 403
    assert client.get(f'/devices/{owner_device}/matches',
                      headers=admin_headers).status_code == 200


def test_formation_excludes_unconsented_minors_and_leaks_no_device_ids(client, admin_headers, membership):
    from datetime import datetime
    from match_tracks.models import Match, Player, Team

    team_code = 'v2-fix-formation-team'
    Team(code=team_code, requires_consent=True).save()

    # 6 devices with formation data; only 5 have consent — the sixth must
    # neither appear nor push the response over the threshold check.
    positions = [(0.06, 0.5), (0.25, 0.2), (0.25, 0.8), (0.55, 0.35), (0.55, 0.65), (0.82, 0.5)]
    device_ids = []
    for index, (mean_x, mean_y) in enumerate(positions):
        device_id = f'v2-fix-formation-device-{index}'
        device_ids.append(device_id)
        consent = datetime.utcnow() if index < 5 else None
        Player(device_id=device_id, name=None, team_code=team_code,
               consent_acknowledged_at=consent).save()
        Match(uuid=new_uuid(), device_id=device_id, team_code=team_code,
              recorded_at=datetime.utcnow(),
              stats={'mean_x': mean_x, 'mean_y': mean_y}).save()

    response = client.get(f'/teams/{team_code}/formation', headers=admin_headers)
    assert response.status_code == 200
    body = response.get_json()
    assert len(body['slots']) == 5  # unconsented sixth excluded entirely
    for slot in body['slots']:
        # No Player.name set → null, never a device-id fragment.
        assert slot['player_name'] is None


def test_batch_auto_join_is_all_or_nothing(client, admin_headers, device_key):
    from match_tracks.models import DeviceTeamMembership

    latitude, longitude = _venue(8)

    # A membership-less device tagging two new teams in one batch joins both.
    fresh_device = register_device(client, admin_headers)
    fresh_headers = device_key('v2-fix-batch-fresh', fresh_device)
    batch = [
        make_extended_session_payload(new_uuid(), '2026-07-13T09:00:00Z',
                                      make_match_track(latitude, longitude, num_points=10),
                                      team_code='v2-batch-team-a'),
        make_extended_session_payload(new_uuid(), '2026-07-13T10:00:00Z',
                                      make_match_track(latitude, longitude, num_points=10),
                                      team_code='v2-batch-team-b'),
    ]
    assert client.post(f'/devices/{fresh_device}/sessions/',
                       json={'sessions': batch},
                       headers=fresh_headers).status_code == 200
    joined = {membership.team_code for membership
              in DeviceTeamMembership.objects(device_id=fresh_device)}
    assert joined == {'v2-batch-team-a', 'v2-batch-team-b'}

    # A device WITH a membership tagging a foreign team gets 400 and no new rows.
    member_device = register_device(client, admin_headers)
    member_headers = device_key('v2-fix-batch-member', member_device)
    first = make_extended_session_payload(new_uuid(), '2026-07-13T09:00:00Z',
                                          make_match_track(latitude, longitude, num_points=10),
                                          team_code='v2-batch-home')
    assert client.post(f'/devices/{member_device}/sessions/',
                       json={'sessions': [first]},
                       headers=member_headers).status_code == 200
    mixed_batch = [
        make_extended_session_payload(new_uuid(), '2026-07-13T11:00:00Z',
                                      make_match_track(latitude, longitude, num_points=10),
                                      team_code='v2-batch-home'),
        make_extended_session_payload(new_uuid(), '2026-07-13T12:00:00Z',
                                      make_match_track(latitude, longitude, num_points=10),
                                      team_code='v2-batch-foreign'),
    ]
    response = client.post(f'/devices/{member_device}/sessions/',
                           json={'sessions': mixed_batch},
                           headers=member_headers)
    assert response.status_code == 400
    assert DeviceTeamMembership.objects(device_id=member_device,
                                        team_code='v2-batch-foreign').count() == 0


# --- Codex second-viewpoint review regressions --------------------------------

def test_foreign_team_product_grants_nothing(client, admin_headers, device_key, app):
    from datetime import datetime, timedelta
    from match_tracks.entitlements import has_active_team_entitlement
    from match_tracks.models import Entitlement

    device_uuid = register_device(client, admin_headers)
    device_headers = device_key('v2-codex-product-key', device_uuid)

    # A receipt for another app's ".team." product is rejected outright.
    app.config['ENTITLEMENT_VERIFIER'] = lambda jws: {
        'product_id': 'com.attacker.app.team.monthly',
        'expires_at': datetime.utcnow() + timedelta(days=365),
        'environment': 'Production',
    }
    response = client.post(f'/devices/{device_uuid}/receipt',
                           json={'jws': 'x.y.z'}, headers=device_headers)
    assert response.status_code == 400

    # Even a directly-inserted foreign entitlement row grants nothing.
    Entitlement(device_id=device_uuid, product_id='com.attacker.app.team.monthly',
                expires_at=datetime.utcnow() + timedelta(days=365)).save()
    with app.app_context():
        assert has_active_team_entitlement(device_uuid) is False


def test_wrong_bundle_receipt_is_rejected(app):
    import base64
    import json as json_module
    from match_tracks.entitlements import InvalidReceipt, default_jws_verifier
    import pytest as pytest_module

    def encode(section):
        return base64.urlsafe_b64encode(
            json_module.dumps(section).encode()).rstrip(b'=').decode()

    foreign_bundle_jws = '.'.join([
        encode({'alg': 'ES256', 'x5c': ['ZmFrZQ==']}),
        encode({'productId': 'com.nicemohawk.MatchTracker.team.monthly',
                'expiresDate': 4102444800000, 'environment': 'Production',
                'bundleId': 'com.attacker.app'}),
        'c2ln',
    ])
    with app.app_context():
        app.config['ENTITLEMENT_ALLOW_UNVERIFIED'] = True
        with pytest_module.raises(InvalidReceipt):
            default_jws_verifier(foreign_bundle_jws)


def test_comment_id_replay_across_matches_conflicts(client, admin_headers, device_key):
    from match_tracks.models import Match

    device_uuid = register_device(client, admin_headers)
    device_headers = device_key('v2-codex-comment-key', device_uuid)
    other_device = register_device(client, admin_headers)
    other_headers = device_key('v2-codex-comment-other', other_device)

    private_match = new_uuid()
    Match(uuid=private_match, device_id=device_uuid, team_code=None).save()
    other_match = new_uuid()
    Match(uuid=other_match, device_id=other_device, team_code=None).save()

    secret_comment_id = new_uuid()
    assert client.post(f'/matches/{private_match}/comments',
                       json={'id': secret_comment_id, 'body': 'secret plan'},
                       headers=device_headers).status_code == 201

    # Replaying the id against a DIFFERENT match must not echo the secret.
    response = client.post(f'/matches/{other_match}/comments',
                           json={'id': secret_comment_id, 'body': 'probe'},
                           headers=other_headers)
    assert response.status_code == 409
    assert 'secret plan' not in response.get_data(as_text=True)


def test_session_and_field_uploads_are_actor_scoped(client, admin_headers, device_key):
    latitude, longitude = _venue(9)
    victim_device = register_device(client, admin_headers)
    device_key('v2-codex-victim-key', victim_device)
    attacker_device = register_device(client, admin_headers)
    attacker_headers = device_key('v2-codex-attacker-key', attacker_device)

    payload = make_extended_session_payload(
        new_uuid(), '2026-07-13T09:00:00Z',
        make_match_track(latitude, longitude, num_points=10),
        team_code='v2-codex-hijack-team')
    assert client.post(f'/devices/{victim_device}/sessions/',
                       json={'sessions': [payload]},
                       headers=attacker_headers).status_code == 403

    field_payload = make_field_payload(new_uuid(), '2026-07-13T09:00:00Z',
                                       make_field_outline(latitude, longitude))
    assert client.post(f'/devices/{victim_device}/fields/',
                       json={'fields': [field_payload]},
                       headers=attacker_headers).status_code == 403

    # Admin keys keep the V1 ability to upload for any device.
    assert client.post(f'/devices/{victim_device}/sessions/',
                       json={'sessions': [payload]},
                       headers=admin_headers).status_code == 200
