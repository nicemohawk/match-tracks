"""Tests for match_tracks.auth and match_tracks.rate_limit.

Throwaway Flask routes are registered directly on the shared app instance
(via app.add_url_rule) so each test can exercise auth.login_required and
rate_limited() without touching match_tracks/routes.py. Every registered
route uses a unique path and endpoint name (derived from the test's request
node id) so routes accumulate safely across the whole test session.
"""

import uuid

from flask import jsonify

from match_tracks.auth import auth, current_principal, principal_may_read_team
from match_tracks.models import Player
from match_tracks.rate_limit import rate_limited, reset_rate_limiter


def _unique_name(prefix):
    return f'{prefix}_{uuid.uuid4().hex}'


def _register_protected_route(app, methods=('GET',)):
    """Register a throwaway route requiring auth.login_required."""
    name = _unique_name('route')
    path = f'/__test__/{name}/'

    def view():
        principal = current_principal()
        return jsonify({'key': principal['key'], 'admin': principal['admin'],
                         'device_id': principal['device_id']})

    view.__name__ = name
    handler = auth.login_required(view)
    app.add_url_rule(path, endpoint=name, view_func=handler, methods=list(methods))
    return path


def _register_rate_limited_route(app, scope):
    """Register a throwaway route that is rate-limited but not authenticated."""
    name = _unique_name('rl_route')
    path = f'/__test__/{name}/'

    @rate_limited(scope)
    def view():
        return jsonify({'ok': True})

    view.__name__ = name
    app.add_url_rule(path, endpoint=name, view_func=view, methods=['POST'])
    return path


# --- Authentication -----------------------------------------------------

def test_valid_admin_key_is_accepted(app, client, admin_headers):
    path = _register_protected_route(app)
    response = client.get(path, headers=admin_headers)
    assert response.status_code == 200
    body = response.get_json()
    assert body['admin'] is True
    assert body['device_id'] is None


def test_unknown_key_is_rejected(app, client):
    path = _register_protected_route(app)
    response = client.get(path, headers={'Authorization': 'APIKey not-a-real-key'})
    assert response.status_code == 401


def test_missing_header_is_rejected(app, client):
    path = _register_protected_route(app)
    response = client.get(path)
    assert response.status_code == 401


def test_device_key_principal_carries_device_id(app, client, device_key):
    path = _register_protected_route(app)
    headers = device_key('device-key-123', 'device-uuid-abc')
    response = client.get(path, headers=headers)
    assert response.status_code == 200
    body = response.get_json()
    assert body['admin'] is False
    assert body['device_id'] == 'device-uuid-abc'


# --- Team scoping --------------------------------------------------------

def test_admin_may_read_any_team(app):
    with app.app_context():
        principal = {'key': 'hi-bob', 'admin': True, 'device_id': None}
        assert principal_may_read_team(principal, 'ANY-TEAM') is True


def test_device_on_team_may_read_own_team(app):
    Player(device_id='device-on-team', name='Alice', team_code='TEAM-A').save()
    with app.app_context():
        principal = {'key': 'k', 'admin': False, 'device_id': 'device-on-team'}
        assert principal_may_read_team(principal, 'TEAM-A') is True


def test_device_on_other_team_may_not_read_team(app):
    Player(device_id='device-on-other-team', name='Bob', team_code='TEAM-B').save()
    with app.app_context():
        principal = {'key': 'k', 'admin': False, 'device_id': 'device-on-other-team'}
        assert principal_may_read_team(principal, 'TEAM-A') is False


def test_device_with_no_player_row_may_not_read_team(app):
    with app.app_context():
        principal = {'key': 'k', 'admin': False, 'device_id': 'device-with-no-player'}
        assert principal_may_read_team(principal, 'TEAM-A') is False


# --- Rate limiting --------------------------------------------------------

def test_rate_limit_blocks_after_limit_then_resets(app, client, admin_headers):
    app.config['RATE_LIMIT_WRITE_PER_MINUTE'] = 3
    path = _register_rate_limited_route(app, 'write')

    responses = [client.post(path, headers=admin_headers) for _ in range(4)]
    statuses = [response.status_code for response in responses]

    assert statuses == [200, 200, 200, 429]
    assert responses[-1].get_json() == {'message': 'rate limit exceeded'}

    reset_rate_limiter()

    response_after_reset = client.post(path, headers=admin_headers)
    assert response_after_reset.status_code == 200


def test_rate_limit_disabled_bypasses_limiting(app, client, admin_headers):
    app.config['RATE_LIMIT_WRITE_PER_MINUTE'] = 1
    app.config['RATE_LIMIT_ENABLED'] = False
    path = _register_rate_limited_route(app, 'write')

    responses = [client.post(path, headers=admin_headers) for _ in range(5)]

    assert all(response.status_code == 200 for response in responses)


def test_rate_limit_buckets_are_per_key(app, client, admin_headers, device_key):
    app.config['RATE_LIMIT_WRITE_PER_MINUTE'] = 1
    path = _register_rate_limited_route(app, 'write')
    device_headers = device_key('other-device-key', 'other-device-uuid')

    first_response = client.post(path, headers=admin_headers)
    second_response_same_key = client.post(path, headers=admin_headers)
    third_response_other_key = client.post(path, headers=device_headers)

    assert first_response.status_code == 200
    assert second_response_same_key.status_code == 429
    assert third_response_other_key.status_code == 200


def test_rate_limit_refill_math_is_deterministic(app, client, admin_headers, monkeypatch):
    import match_tracks.rate_limit as rate_limit_module

    fake_now = [1000.0]
    monkeypatch.setattr(rate_limit_module.time, 'monotonic', lambda: fake_now[0])

    app.config['RATE_LIMIT_WRITE_PER_MINUTE'] = 3
    path = _register_rate_limited_route(app, 'write')

    # Exhaust the 3-token bucket.
    statuses = [client.post(path, headers=admin_headers).status_code for _ in range(3)]
    assert statuses == [200, 200, 200]
    assert client.post(path, headers=admin_headers).status_code == 429

    # Advance the simulated clock by 20 seconds: refill rate is 3/60 = 0.05
    # tokens/sec, so about 1 token (0.05 * 20 = 1.0) should be available.
    fake_now[0] += 20.0

    assert client.post(path, headers=admin_headers).status_code == 200
    assert client.post(path, headers=admin_headers).status_code == 429


# --- Dev mode: accept any APIKey (local testing only) -------------------------

def test_unknown_key_rejected_when_dev_mode_off(app):
    """Baseline: with the override off (the default), unknown keys are 401."""
    path = _register_protected_route(app)
    assert app.config.get('DEV_ACCEPT_ANY_API_KEY') in (None, False)
    client = app.test_client()
    resp = client.get(path, headers={'Authorization': 'APIKey totally-made-up'})
    assert resp.status_code == 401


def test_dev_mode_accepts_any_key_as_admin(app):
    """With the override on, any unknown key authenticates as an admin dev principal."""
    from match_tracks import auth as auth_module
    auth_module._dev_mode_warning_emitted = False
    app.config['DEV_ACCEPT_ANY_API_KEY'] = True
    path = _register_protected_route(app)
    try:
        client = app.test_client()
        resp = client.get(path, headers={'Authorization': 'APIKey anything-goes'})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['admin'] is True
        assert body['device_id'] is None
        assert body['key'] == 'anything-goes'
        # An empty token is still rejected even in dev mode.
        assert client.get(path, headers={'Authorization': 'APIKey '}).status_code == 401
    finally:
        app.config['DEV_ACCEPT_ANY_API_KEY'] = False


def test_dev_mode_preserves_real_identity_of_known_keys(app, device_key):
    """Configured device keys keep their device identity even with dev mode on."""
    app.config['DEV_ACCEPT_ANY_API_KEY'] = True
    device_uuid = '11111111-2222-3333-4444-555555555555'
    headers = device_key('a-real-device-key', device_uuid)
    path = _register_protected_route(app)
    try:
        resp = app.test_client().get(path, headers=headers)
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['admin'] is False
        assert body['device_id'] == device_uuid
    finally:
        app.config['DEV_ACCEPT_ANY_API_KEY'] = False


def test_dev_mode_hard_disabled_under_prod_env(app, monkeypatch):
    """ENV=prod overrides the flag: an unknown key is rejected even if the
    config accidentally sets DEV_ACCEPT_ANY_API_KEY=True on a prod box."""
    monkeypatch.setenv('ENV', 'prod')
    app.config['DEV_ACCEPT_ANY_API_KEY'] = True
    path = _register_protected_route(app)
    try:
        resp = app.test_client().get(path, headers={'Authorization': 'APIKey anything-goes'})
        assert resp.status_code == 401
    finally:
        app.config['DEV_ACCEPT_ANY_API_KEY'] = False
