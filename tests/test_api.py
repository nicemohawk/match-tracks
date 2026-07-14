"""Acceptance tests for the new Match Tracks API surface introduced by the
Flask backend upgrade: extended match sessions, the matches list/detail
endpoints, team stats, the community field database, and rate limiting.

None of these routes exist yet at the time this suite is written — it is the
executable spec that a later agent implements against. Every test below is
numbered in its docstring to match the acceptance criteria in the task spec.

Each test picks its own venue (base coordinate plus an index-based offset,
see ``_venue``) at least 0.05 degrees away from every other test's venue, so
that community-field geometry matching in one test can never accidentally
pick up an observation created by another test.
"""

from tests.helpers import make_event, make_extended_session_payload, make_field_outline, \
    make_field_payload, make_match_stats, make_match_track, make_session_payload, \
    meters_per_degree_longitude, new_uuid, offset_coordinate, register_device

BASE_LATITUDE = 39.33
BASE_LONGITUDE = -82.10
VENUE_SPACING_DEGREES = 0.5


def _venue(index):
    """A unique (lat, lon) test venue, well outside any other venue's radius."""
    return (BASE_LATITUDE + index * VENUE_SPACING_DEGREES,
            BASE_LONGITUDE + index * VENUE_SPACING_DEGREES)


# (3) Extended session round-trips via GET matches (list + detail).
def test_extended_session_round_trips_through_matches_endpoints(client, admin_headers, device_key):
    device_uuid = register_device(client, admin_headers)
    device_headers = device_key('device-key-extended-round-trip', device_uuid)
    field_latitude, field_longitude = _venue(1)

    field_uuid = new_uuid()
    outline_coordinates = make_field_outline(field_latitude, field_longitude)
    field_response = client.post(
        f'/devices/{device_uuid}/fields/',
        json={'fields': [make_field_payload(field_uuid, '2026-07-13T09:00:00Z', outline_coordinates)]},
        headers=device_headers,
    )
    assert field_response.status_code == 200

    session_uuid = new_uuid()
    match_track = make_match_track(field_latitude, field_longitude, num_points=400)
    match_events = [
        make_event('goalFor', '2026-07-13T10:20:00Z'),
        make_event('assist', '2026-07-13T10:20:00Z'),
    ]
    match_stats = make_match_stats(
        total_distance_m=9500.0, time_on_pitch_s=5400.0, sprint_count=12, run_count=40,
        workrate_score=7.5, avg_hr=150, position_role='midfielder', position_side='center',
    )
    session_payload = make_extended_session_payload(
        session_uuid, '2026-07-13T10:00:00Z', match_track,
        duration_s=5400.0, field_uuid=field_uuid, team_code='home',
        player_name='Alice', events=match_events, stats=match_stats,
    )
    session_response = client.post(
        f'/devices/{device_uuid}/sessions/',
        json={'sessions': [session_payload]},
        headers=device_headers,
    )
    assert session_response.status_code == 200
    assert 'added_sessions' in session_response.get_json()

    matches_response = client.get(f'/devices/{device_uuid}/matches', headers=device_headers)
    assert matches_response.status_code == 200
    matches = matches_response.get_json()['matches']
    assert len(matches) == 1
    match_summary = matches[0]
    assert match_summary['uuid'] == session_uuid
    assert match_summary['recorded_at'] == '2026-07-13T10:00:00Z'
    assert match_summary['duration_s'] == 5400.0
    assert match_summary['field_uuid'] == field_uuid
    assert match_summary['team_code'] == 'home'
    assert match_summary['stats']['total_distance_m'] == 9500.0
    assert 'track' not in match_summary
    assert 'events' not in match_summary

    detail_response = client.get(f'/devices/{device_uuid}/matches/{session_uuid}', headers=device_headers)
    assert detail_response.status_code == 200
    match_detail = detail_response.get_json()['match']
    assert match_detail['track']['coordinates'] == match_track
    assert len(match_detail['events']) == 2


# (4) Re-POST of the same session uuid upserts rather than duplicating.
def test_reposting_same_session_uuid_upserts_the_match(client, admin_headers, device_key):
    device_uuid = register_device(client, admin_headers)
    device_headers = device_key('device-key-upsert', device_uuid)
    latitude, longitude = _venue(2)
    session_uuid = new_uuid()
    track = make_match_track(latitude, longitude, num_points=10)

    first_payload = make_extended_session_payload(
        session_uuid, '2026-07-13T09:00:00Z', track, duration_s=3000.0, team_code='home')
    first_response = client.post(
        f'/devices/{device_uuid}/sessions/', json={'sessions': [first_payload]}, headers=device_headers)
    assert first_response.status_code == 200

    second_payload = make_extended_session_payload(
        session_uuid, '2026-07-13T09:00:00Z', track, duration_s=5400.0, team_code='home')
    second_response = client.post(
        f'/devices/{device_uuid}/sessions/', json={'sessions': [second_payload]}, headers=device_headers)
    assert second_response.status_code == 200
    assert 'added_sessions' in second_response.get_json()

    matches_response = client.get(f'/devices/{device_uuid}/matches', headers=device_headers)
    matches_body = matches_response.get_json()
    assert matches_body['total'] == 1
    assert len(matches_body['matches']) == 1
    assert matches_body['matches'][0]['duration_s'] == 5400.0


# (5) Two devices training the same physical field merge into one community
# field; a session referencing the alias uuid resolves to the canonical uuid.
def test_duplicate_field_observations_merge_and_alias_resolves_to_canonical(client, admin_headers, device_key):
    device_a_uuid = register_device(client, admin_headers, name='Device A')
    device_a_headers = device_key('device-key-field-a', device_a_uuid)
    device_b_uuid = register_device(client, admin_headers, name='Device B')
    device_b_headers = device_key('device-key-field-b', device_b_uuid)

    latitude, longitude = _venue(3)

    first_field_uuid = new_uuid()
    first_outline = make_field_outline(latitude, longitude)
    first_response = client.post(
        f'/devices/{device_a_uuid}/fields/',
        json={'fields': [make_field_payload(first_field_uuid, '2026-07-13T08:00:00Z', first_outline)]},
        headers=device_a_headers,
    )
    assert first_response.status_code == 200

    second_field_uuid = new_uuid()
    second_outline = make_field_outline(latitude, longitude)
    second_response = client.post(
        f'/devices/{device_b_uuid}/fields/',
        json={'fields': [make_field_payload(second_field_uuid, '2026-07-13T08:05:00Z', second_outline)]},
        headers=device_b_headers,
    )
    assert second_response.status_code == 200

    nearby_response = client.get(
        f'/fields/nearby?lat={latitude}&lon={longitude}&radius_m=2000', headers=device_a_headers)
    assert nearby_response.status_code == 200
    nearby_fields = nearby_response.get_json()['fields']
    assert len(nearby_fields) == 1
    assert nearby_fields[0]['observation_count'] == 2
    canonical_field_uuid = nearby_fields[0]['uuid']
    assert canonical_field_uuid in (first_field_uuid, second_field_uuid)

    session_uuid = new_uuid()
    session_track = make_match_track(latitude, longitude, num_points=10)
    session_payload = make_extended_session_payload(
        session_uuid, '2026-07-13T08:10:00Z', session_track, field_uuid=second_field_uuid)
    session_response = client.post(
        f'/devices/{device_a_uuid}/sessions/', json={'sessions': [session_payload]}, headers=device_a_headers)
    assert session_response.status_code == 200

    detail_response = client.get(f'/devices/{device_a_uuid}/matches/{session_uuid}', headers=device_a_headers)
    assert detail_response.status_code == 200
    assert detail_response.get_json()['match']['field_uuid'] == canonical_field_uuid


# (6) Team stats: exact per-player aggregates across matches with mixed events.
def test_team_stats_reports_exact_per_player_aggregates(client, admin_headers, device_key):
    device_alice_uuid = register_device(client, admin_headers, name='Alice Watch')
    alice_headers = device_key('device-key-alice', device_alice_uuid)
    device_bob_uuid = register_device(client, admin_headers, name='Bob Watch')
    bob_headers = device_key('device-key-bob', device_bob_uuid)

    latitude, longitude = _venue(4)
    small_track = make_match_track(latitude, longitude, num_points=10)

    alice_match_one = make_extended_session_payload(
        new_uuid(), '2026-07-13T09:00:00Z', small_track, team_code='red', player_name='Alice',
        stats=make_match_stats(total_distance_m=9000.0, time_on_pitch_s=3000.0,
                                sprint_count=10, workrate_score=7.0),
        events=[
            make_event('goalFor', '2026-07-13T09:20:00Z'),
            make_event('assist', '2026-07-13T09:30:00Z'),
            make_event('flag', '2026-07-13T09:40:00Z'),
        ],
    )
    alice_match_two = make_extended_session_payload(
        new_uuid(), '2026-07-13T11:00:00Z', small_track, team_code='red', player_name='Alice',
        stats=make_match_stats(total_distance_m=8000.0, time_on_pitch_s=2700.0,
                                sprint_count=8, workrate_score=6.0),
        events=[
            make_event('goalMine', '2026-07-13T11:20:00Z'),
            make_event('subIn', '2026-07-13T11:00:00Z'),
            make_event('subOut', '2026-07-13T11:45:00Z'),
        ],
    )
    bob_match_one = make_extended_session_payload(
        new_uuid(), '2026-07-13T10:00:00Z', small_track, team_code='red', player_name='Bob',
        stats=make_match_stats(total_distance_m=10000.0, time_on_pitch_s=5400.0,
                                sprint_count=15, workrate_score=8.0),
        events=[
            make_event('goalAgainst', '2026-07-13T10:20:00Z'),
            make_event('assist', '2026-07-13T10:30:00Z'),
            make_event('assist', '2026-07-13T10:40:00Z'),
        ],
    )

    for payload, headers, device_uuid in (
        (alice_match_one, alice_headers, device_alice_uuid),
        (alice_match_two, alice_headers, device_alice_uuid),
        (bob_match_one, bob_headers, device_bob_uuid),
    ):
        response = client.post(
            f'/devices/{device_uuid}/sessions/', json={'sessions': [payload]}, headers=headers)
        assert response.status_code == 200

    stats_response = client.get('/teams/red/stats', headers=admin_headers)
    assert stats_response.status_code == 200
    stats_body = stats_response.get_json()
    assert stats_body['team_code'] == 'red'
    players_by_device_id = {player['device_id']: player for player in stats_body['players']}

    alice_stats = players_by_device_id[device_alice_uuid]
    assert alice_stats['player_name'] == 'Alice'
    assert alice_stats['matches_played'] == 2
    assert alice_stats['total_minutes'] == 95.0
    assert alice_stats['total_distance_m'] == 17000.0
    assert alice_stats['total_sprints'] == 18
    assert alice_stats['avg_workrate_score'] == 6.5
    assert alice_stats['goals'] == 2  # one goalFor + one goalMine
    assert alice_stats['assists'] == 1

    bob_stats = players_by_device_id[device_bob_uuid]
    assert bob_stats['player_name'] == 'Bob'
    assert bob_stats['matches_played'] == 1
    assert bob_stats['total_minutes'] == 90.0
    assert bob_stats['total_distance_m'] == 10000.0
    assert bob_stats['total_sprints'] == 15
    assert bob_stats['avg_workrate_score'] == 8.0
    assert bob_stats['goals'] == 0  # goalAgainst must not count
    assert bob_stats['assists'] == 2


# (7) Team stats scoping: same-team device key allowed, other-team denied, admin allowed.
def test_team_stats_endpoint_scopes_access_by_team_membership(client, admin_headers, device_key):
    device_red_uuid = register_device(client, admin_headers, name='Red Player Device')
    red_headers = device_key('device-key-red-team', device_red_uuid)
    device_blue_uuid = register_device(client, admin_headers, name='Blue Player Device')
    blue_headers = device_key('device-key-blue-team', device_blue_uuid)

    latitude, longitude = _venue(5)
    small_track = make_match_track(latitude, longitude, num_points=10)

    red_payload = make_extended_session_payload(
        new_uuid(), '2026-07-13T09:00:00Z', small_track, team_code='red', player_name='Riley')
    blue_payload = make_extended_session_payload(
        new_uuid(), '2026-07-13T09:00:00Z', small_track, team_code='blue', player_name='Blake')

    assert client.post(f'/devices/{device_red_uuid}/sessions/', json={'sessions': [red_payload]},
                        headers=red_headers).status_code == 200
    assert client.post(f'/devices/{device_blue_uuid}/sessions/', json={'sessions': [blue_payload]},
                        headers=blue_headers).status_code == 200

    other_team_response = client.get('/teams/red/stats', headers=blue_headers)
    assert other_team_response.status_code == 403

    same_team_response = client.get('/teams/red/stats', headers=red_headers)
    assert same_team_response.status_code == 200

    admin_response = client.get('/teams/red/stats', headers=admin_headers)
    assert admin_response.status_code == 200

    unknown_team_response = client.get('/teams/does-not-exist/stats', headers=admin_headers)
    assert unknown_team_response.status_code == 404


# (8) Matches list pagination.
def test_matches_pagination_returns_the_requested_page(client, admin_headers, device_key):
    device_uuid = register_device(client, admin_headers)
    device_headers = device_key('device-key-pagination', device_uuid)
    latitude, longitude = _venue(6)
    small_track = make_match_track(latitude, longitude, num_points=10)

    oldest_session_uuid = new_uuid()
    middle_session_uuid = new_uuid()
    newest_session_uuid = new_uuid()
    for session_uuid, recorded_at in (
        (oldest_session_uuid, '2026-07-13T08:00:00Z'),
        (middle_session_uuid, '2026-07-13T09:00:00Z'),
        (newest_session_uuid, '2026-07-13T10:00:00Z'),
    ):
        payload = make_session_payload(session_uuid, recorded_at, small_track)
        response = client.post(
            f'/devices/{device_uuid}/sessions/', json={'sessions': [payload]}, headers=device_headers)
        assert response.status_code == 200

    page_response = client.get(
        f'/devices/{device_uuid}/matches?limit=1&offset=1', headers=device_headers)
    assert page_response.status_code == 200
    page_body = page_response.get_json()
    assert page_body['total'] == 3
    assert page_body['limit'] == 1
    assert page_body['offset'] == 1
    assert len(page_body['matches']) == 1
    assert page_body['matches'][0]['uuid'] == middle_session_uuid


# (9) Health check.
def test_health_endpoint_reports_ok_without_authentication(client):
    response = client.get('/health')
    assert response.status_code == 200
    assert response.get_json() == {'status': 'ok'}


# (10) Rate limiting on writes.
def test_rate_limit_returns_429_after_burst_of_writes(client, admin_headers, app, device_key):
    app.config['RATE_LIMIT_WRITE_PER_MINUTE'] = 3
    device_uuid = register_device(client, admin_headers)
    device_headers = device_key('device-key-rate-limit', device_uuid)
    latitude, longitude = _venue(7)

    for attempt_number in range(3):
        track = make_match_track(latitude, longitude, num_points=5, seed=attempt_number)
        payload = make_session_payload(new_uuid(), '2026-07-13T09:00:00Z', track)
        response = client.post(
            f'/devices/{device_uuid}/sessions/', json={'sessions': [payload]}, headers=device_headers)
        assert response.status_code == 200

    fourth_track = make_match_track(latitude, longitude, num_points=5, seed=99)
    fourth_payload = make_session_payload(new_uuid(), '2026-07-13T09:00:00Z', fourth_track)
    fourth_response = client.post(
        f'/devices/{device_uuid}/sessions/', json={'sessions': [fourth_payload]}, headers=device_headers)
    assert fourth_response.status_code == 429


# (11) Observation weighting: center shifts by 1/(n+1) of the offset, confidence rises.
def test_field_center_shifts_by_weighted_average_as_observations_accumulate(client, admin_headers, device_key):
    device_uuid = register_device(client, admin_headers)
    device_headers = device_key('device-key-weighting', device_uuid)
    latitude, longitude = _venue(8)

    initial_outline = make_field_outline(latitude, longitude)
    initial_response = client.post(
        f'/devices/{device_uuid}/fields/',
        json={'fields': [make_field_payload(new_uuid(), '2026-07-13T07:00:00Z', initial_outline)]},
        headers=device_headers,
    )
    assert initial_response.status_code == 200

    for _ in range(8):
        additional_outline = make_field_outline(latitude, longitude)
        additional_response = client.post(
            f'/devices/{device_uuid}/fields/',
            json={'fields': [make_field_payload(new_uuid(), '2026-07-13T07:00:00Z', additional_outline)]},
            headers=device_headers,
        )
        assert additional_response.status_code == 200

    before_offset_response = client.get(
        f'/fields/nearby?lat={latitude}&lon={longitude}&radius_m=2000', headers=device_headers)
    fields_before_offset = before_offset_response.get_json()['fields']
    assert len(fields_before_offset) == 1
    field_before_offset = fields_before_offset[0]
    assert field_before_offset['observation_count'] == 9
    confidence_before_offset = field_before_offset['confidence']
    center_longitude_before_offset = field_before_offset['rectangle']['center_lon']

    east_offset_m = 20.0
    offset_latitude, offset_longitude = offset_coordinate(latitude, longitude, east_offset_m=east_offset_m)
    offset_outline = make_field_outline(offset_latitude, offset_longitude)
    offset_response = client.post(
        f'/devices/{device_uuid}/fields/',
        json={'fields': [make_field_payload(new_uuid(), '2026-07-13T07:00:00Z', offset_outline)]},
        headers=device_headers,
    )
    assert offset_response.status_code == 200

    after_offset_response = client.get(
        f'/fields/nearby?lat={latitude}&lon={longitude}&radius_m=2000', headers=device_headers)
    fields_after_offset = after_offset_response.get_json()['fields']
    assert len(fields_after_offset) == 1
    field_after_offset = fields_after_offset[0]
    assert field_after_offset['observation_count'] == 10
    assert field_after_offset['confidence'] > confidence_before_offset

    center_longitude_after_offset = field_after_offset['rectangle']['center_lon']
    actual_shift_m = ((center_longitude_after_offset - center_longitude_before_offset)
                       * meters_per_degree_longitude(latitude))
    expected_shift_m = east_offset_m / 10.0  # weight (9 existing, 1 new) -> 1/10 of the delta
    assert expected_shift_m * 0.8 <= actual_shift_m <= expected_shift_m * 1.2


# (12) Heading wraparound merges fields across the 180-degree seam.
def test_heading_wraparound_merges_fields_across_the_180_degree_seam(client, admin_headers, device_key):
    device_uuid = register_device(client, admin_headers)
    device_headers = device_key('device-key-heading-wrap', device_uuid)
    latitude, longitude = _venue(9)

    outline_at_179_degrees = make_field_outline(latitude, longitude, heading_deg=179.0)
    first_response = client.post(
        f'/devices/{device_uuid}/fields/',
        json={'fields': [make_field_payload(new_uuid(), '2026-07-13T07:00:00Z', outline_at_179_degrees)]},
        headers=device_headers,
    )
    assert first_response.status_code == 200

    outline_at_1_degree = make_field_outline(latitude, longitude, heading_deg=1.0)
    second_response = client.post(
        f'/devices/{device_uuid}/fields/',
        json={'fields': [make_field_payload(new_uuid(), '2026-07-13T07:05:00Z', outline_at_1_degree)]},
        headers=device_headers,
    )
    assert second_response.status_code == 200

    nearby_response = client.get(
        f'/fields/nearby?lat={latitude}&lon={longitude}&radius_m=2000', headers=device_headers)
    assert nearby_response.status_code == 200
    fields = nearby_response.get_json()['fields']
    assert len(fields) == 1
    assert fields[0]['observation_count'] == 2
    folded_heading_deg = fields[0]['rectangle']['heading_deg']
    assert min(folded_heading_deg, 180.0 - folded_heading_deg) < 5.0

    outline_at_181_degrees = make_field_outline(latitude, longitude, heading_deg=181.0)
    third_response = client.post(
        f'/devices/{device_uuid}/fields/',
        json={'fields': [make_field_payload(new_uuid(), '2026-07-13T07:10:00Z', outline_at_181_degrees)]},
        headers=device_headers,
    )
    assert third_response.status_code == 200

    final_nearby_response = client.get(
        f'/fields/nearby?lat={latitude}&lon={longitude}&radius_m=2000', headers=device_headers)
    final_fields = final_nearby_response.get_json()['fields']
    assert len(final_fields) == 1  # still one field, not a second one
    assert final_fields[0]['observation_count'] == 3


# (13) A field-uuid-less session track both bumps an existing field's
# observation count and resolves the match's field_uuid to it; the same
# track at a fresh venue creates a brand-new inferred field instead.
def test_session_track_without_field_uuid_infers_field_association(client, admin_headers, device_key):
    device_uuid = register_device(client, admin_headers)
    device_headers = device_key('device-key-inference', device_uuid)
    latitude, longitude = _venue(10)

    field_uuid = new_uuid()
    outline_coordinates = make_field_outline(latitude, longitude)
    field_response = client.post(
        f'/devices/{device_uuid}/fields/',
        json={'fields': [make_field_payload(field_uuid, '2026-07-13T07:00:00Z', outline_coordinates)]},
        headers=device_headers,
    )
    assert field_response.status_code == 200

    session_uuid = new_uuid()
    match_track = make_match_track(latitude, longitude, num_points=400)
    session_payload = make_session_payload(session_uuid, '2026-07-13T08:00:00Z', match_track)
    session_response = client.post(
        f'/devices/{device_uuid}/sessions/', json={'sessions': [session_payload]}, headers=device_headers)
    assert session_response.status_code == 200

    nearby_response = client.get(
        f'/fields/nearby?lat={latitude}&lon={longitude}&radius_m=2000', headers=device_headers)
    fields = nearby_response.get_json()['fields']
    assert len(fields) == 1
    assert fields[0]['observation_count'] == 2

    detail_response = client.get(f'/devices/{device_uuid}/matches/{session_uuid}', headers=device_headers)
    assert detail_response.get_json()['match']['field_uuid'] == field_uuid

    fresh_latitude, fresh_longitude = _venue(11)
    fresh_track = make_match_track(fresh_latitude, fresh_longitude, num_points=400)
    fresh_session_uuid = new_uuid()
    fresh_payload = make_session_payload(fresh_session_uuid, '2026-07-13T08:30:00Z', fresh_track)
    fresh_response = client.post(
        f'/devices/{device_uuid}/sessions/', json={'sessions': [fresh_payload]}, headers=device_headers)
    assert fresh_response.status_code == 200

    fresh_nearby_response = client.get(
        f'/fields/nearby?lat={fresh_latitude}&lon={fresh_longitude}&radius_m=2000', headers=device_headers)
    fresh_fields = fresh_nearby_response.get_json()['fields']
    assert len(fresh_fields) == 1
    assert fresh_fields[0]['source'] == 'inferred'
    assert fresh_fields[0]['outline'] is None


# (14) Tracks that are too short, or whose fitted rectangle is implausibly
# small, must not create any field observation.
def test_short_or_undersized_tracks_do_not_create_field_observations(client, admin_headers, device_key):
    device_uuid = register_device(client, admin_headers)
    device_headers = device_key('device-key-no-observation', device_uuid)

    short_latitude, short_longitude = _venue(12)
    short_track = make_match_track(short_latitude, short_longitude, num_points=50)
    short_response = client.post(
        f'/devices/{device_uuid}/sessions/',
        json={'sessions': [make_session_payload(new_uuid(), '2026-07-13T07:00:00Z', short_track)]},
        headers=device_headers,
    )
    assert short_response.status_code == 200
    short_nearby_response = client.get(
        f'/fields/nearby?lat={short_latitude}&lon={short_longitude}&radius_m=2000', headers=device_headers)
    assert short_nearby_response.get_json()['fields'] == []

    tiny_latitude, tiny_longitude = _venue(13)
    tiny_track = make_match_track(tiny_latitude, tiny_longitude, length_m=20.0, width_m=10.0, num_points=400)
    tiny_response = client.post(
        f'/devices/{device_uuid}/sessions/',
        json={'sessions': [make_session_payload(new_uuid(), '2026-07-13T07:00:00Z', tiny_track)]},
        headers=device_headers,
    )
    assert tiny_response.status_code == 200
    tiny_nearby_response = client.get(
        f'/fields/nearby?lat={tiny_latitude}&lon={tiny_longitude}&radius_m=2000', headers=device_headers)
    assert tiny_nearby_response.get_json()['fields'] == []


# (15) fields/nearby respects radius and min_confidence, sorts ascending by
# distance, and validates its required lat/lon parameters and authentication.
def test_fields_nearby_filters_by_radius_min_confidence_and_sorts_by_distance(client, admin_headers, device_key):
    device_uuid = register_device(client, admin_headers)
    device_headers = device_key('device-key-nearby-filters', device_uuid)
    latitude, longitude = _venue(14)

    near_outline = make_field_outline(latitude, longitude)
    near_response = client.post(
        f'/devices/{device_uuid}/fields/',
        json={'fields': [make_field_payload(new_uuid(), '2026-07-13T07:00:00Z', near_outline)]},
        headers=device_headers,
    )
    assert near_response.status_code == 200

    far_latitude, far_longitude = offset_coordinate(latitude, longitude, north_offset_m=5000.0)
    far_outline = make_field_outline(far_latitude, far_longitude)
    far_response = client.post(
        f'/devices/{device_uuid}/fields/',
        json={'fields': [make_field_payload(new_uuid(), '2026-07-13T07:00:00Z', far_outline)]},
        headers=device_headers,
    )
    assert far_response.status_code == 200

    radius_excludes_far_response = client.get(
        f'/fields/nearby?lat={latitude}&lon={longitude}&radius_m=3000', headers=device_headers)
    assert radius_excludes_far_response.status_code == 200
    fields_within_radius = radius_excludes_far_response.get_json()['fields']
    assert len(fields_within_radius) == 1
    assert all(field['distance_m'] <= 3000 for field in fields_within_radius)

    wider_radius_response = client.get(
        f'/fields/nearby?lat={latitude}&lon={longitude}&radius_m=6000', headers=device_headers)
    both_fields = wider_radius_response.get_json()['fields']
    assert len(both_fields) == 2
    distances_m = [field['distance_m'] for field in both_fields]
    assert distances_m == sorted(distances_m)

    high_min_confidence_response = client.get(
        f'/fields/nearby?lat={latitude}&lon={longitude}&radius_m=3000&min_confidence=0.99',
        headers=device_headers,
    )
    assert high_min_confidence_response.get_json()['fields'] == []

    low_min_confidence_response = client.get(
        f'/fields/nearby?lat={latitude}&lon={longitude}&radius_m=3000&min_confidence=0.0',
        headers=device_headers,
    )
    assert len(low_min_confidence_response.get_json()['fields']) == 1

    missing_lat_response = client.get(
        f'/fields/nearby?lon={longitude}&radius_m=3000', headers=device_headers)
    assert missing_lat_response.status_code == 400

    unauthenticated_response = client.get(f'/fields/nearby?lat={latitude}&lon={longitude}')
    assert unauthenticated_response.status_code == 401


# (16) A device that has never uploaded anything can still see fields
# trained by other devices ("community" visibility).
def test_new_devices_can_see_fields_trained_by_other_devices(client, admin_headers, device_key):
    trainer_device_uuid = register_device(client, admin_headers, name='Trainer Device')
    trainer_device_headers = device_key('device-key-trainer', trainer_device_uuid)
    latitude, longitude = _venue(15)

    outline_coordinates = make_field_outline(latitude, longitude)
    train_response = client.post(
        f'/devices/{trainer_device_uuid}/fields/',
        json={'fields': [make_field_payload(new_uuid(), '2026-07-13T07:00:00Z', outline_coordinates)]},
        headers=trainer_device_headers,
    )
    assert train_response.status_code == 200

    fresh_device_uuid = register_device(client, admin_headers, name='Fresh Observer Device')
    fresh_device_headers = device_key('device-key-fresh-observer', fresh_device_uuid)

    nearby_response = client.get(
        f'/fields/nearby?lat={latitude}&lon={longitude}&radius_m=2000', headers=fresh_device_headers)
    assert nearby_response.status_code == 200
    fields = nearby_response.get_json()['fields']
    assert len(fields) == 1
    assert fields[0]['observation_count'] == 1
