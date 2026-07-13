"""Acceptance tests for the legacy (pre-upgrade) device endpoints.

These endpoints (POST/GET /devices/{uuid}/sessions/ and
POST/GET /devices/{uuid}/fields/) must keep working exactly as they did
before the backend upgrade: existing watch firmware in the field posts
sessions and fields in this shape and cannot be updated in lockstep with the
server, so nothing here may become a breaking change.

Acceptance criteria covered (see task spec):
    (1) legacy-only fields POST succeeds with 'added_fields'
    (2) legacy-only sessions POST succeeds with 'added_sessions'; GET
        sessions echoes the stored track; unknown extra keys are tolerated;
        unauthenticated POST is rejected with 401.
"""

from tests.helpers import make_field_outline, make_match_track, make_field_payload, \
    make_session_payload, register_device

BASE_LATITUDE = 39.33
BASE_LONGITUDE = -82.10


def test_legacy_fields_upload_returns_added_fields(client, admin_headers):
    """(1) A legacy-only fields POST (no extended keys) succeeds and reports 'added_fields'."""
    device_uuid = register_device(client, admin_headers)
    outline_coordinates = make_field_outline(BASE_LATITUDE, BASE_LONGITUDE)
    field_uuid = "11111111-1111-1111-1111-111111111111"
    field_payload = make_field_payload(field_uuid, '2026-07-13T18:04:00Z', outline_coordinates)

    response = client.post(
        f'/devices/{device_uuid}/fields/',
        json={'fields': [field_payload]},
        headers=admin_headers,
    )

    assert response.status_code == 200
    response_body = response.get_json()
    assert 'added_fields' in response_body
    assert len(response_body['added_fields']) == 1


def test_legacy_sessions_upload_returns_added_sessions(client, admin_headers):
    """(2) A legacy-only sessions POST (no extended keys) succeeds and reports 'added_sessions'."""
    device_uuid = register_device(client, admin_headers)
    coordinates = make_match_track(BASE_LATITUDE, BASE_LONGITUDE + 1.0, num_points=10)
    session_uuid = "22222222-2222-2222-2222-222222222222"
    session_payload = make_session_payload(session_uuid, '2026-07-13T18:04:00Z', coordinates)

    response = client.post(
        f'/devices/{device_uuid}/sessions/',
        json={'sessions': [session_payload]},
        headers=admin_headers,
    )

    assert response.status_code == 200
    response_body = response.get_json()
    assert 'added_sessions' in response_body
    assert len(response_body['added_sessions']) == 1


def test_legacy_sessions_get_echoes_stored_track(client, admin_headers):
    """(2) GET /devices/{uuid}/sessions/ echoes back the exact coordinates that were uploaded."""
    device_uuid = register_device(client, admin_headers)
    coordinates = make_match_track(BASE_LATITUDE, BASE_LONGITUDE + 2.0, num_points=10)
    session_uuid = "33333333-3333-3333-3333-333333333333"
    session_payload = make_session_payload(session_uuid, '2026-07-13T18:04:00Z', coordinates)

    post_response = client.post(
        f'/devices/{device_uuid}/sessions/',
        json={'sessions': [session_payload]},
        headers=admin_headers,
    )
    assert post_response.status_code == 200

    get_response = client.get(f'/devices/{device_uuid}/sessions/')

    assert get_response.status_code == 200
    stored_sessions = get_response.get_json()['sessions']
    assert len(stored_sessions) == 1
    assert stored_sessions[0]['track']['coordinates'] == coordinates


def test_legacy_sessions_upload_tolerates_unknown_extra_keys(client, admin_headers):
    """(2) Extra/unknown keys on a session object must not cause an error."""
    device_uuid = register_device(client, admin_headers)
    coordinates = make_match_track(BASE_LATITUDE, BASE_LONGITUDE + 3.0, num_points=10)
    session_uuid = "44444444-4444-4444-4444-444444444444"
    session_payload = make_session_payload(
        session_uuid, '2026-07-13T18:04:00Z', coordinates,
        firmware_version='7.2.1', an_unrecognized_future_field={'nested': True},
    )

    response = client.post(
        f'/devices/{device_uuid}/sessions/',
        json={'sessions': [session_payload]},
        headers=admin_headers,
    )

    assert response.status_code == 200
    assert 'added_sessions' in response.get_json()


def test_legacy_sessions_upload_requires_authentication(client, admin_headers):
    """(2) An unauthenticated POST to the sessions endpoint is rejected with 401."""
    device_uuid = register_device(client, admin_headers)
    coordinates = make_match_track(BASE_LATITUDE, BASE_LONGITUDE + 4.0, num_points=10)
    session_uuid = "55555555-5555-5555-5555-555555555555"
    session_payload = make_session_payload(session_uuid, '2026-07-13T18:04:00Z', coordinates)

    response = client.post(
        f'/devices/{device_uuid}/sessions/',
        json={'sessions': [session_payload]},
    )

    assert response.status_code == 401
