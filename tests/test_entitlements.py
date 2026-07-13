"""Tests for match_tracks.entitlements (V2 §7): receipt verification,
entitlement summaries, require_team_entitlement enforcement, and the
default (unverified-friendly) JWS decoder.

Device identifiers in this file are prefixed with ``enttest-`` to avoid
colliding with fixtures created by other test files running in the same
process.
"""

import base64
import importlib.util
import json
import uuid as uuid_module
from datetime import datetime, timedelta

import pytest
from flask import jsonify

from match_tracks.auth import auth
from match_tracks.entitlements import (InvalidReceipt, VerifierUnavailable,
                                      default_jws_verifier, require_team_entitlement)

TIMESTAMP_FORMAT = '%Y-%m-%dT%H:%M:%SZ'
TEAM_PRODUCT_ID = 'com.nicemohawk.MatchTracker.team.monthly'
CRYPTOGRAPHY_AVAILABLE = importlib.util.find_spec('cryptography') is not None


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _build_fake_jws(product_id=TEAM_PRODUCT_ID, expires_at=None, environment='Sandbox'):
    """An unsigned StoreKit-shaped JWS: fake x5c leaf, arbitrary signature."""
    if expires_at is None:
        expires_at = datetime.utcnow() + timedelta(days=30)
    # expires_at is a naive UTC datetime; .timestamp() would misinterpret it as
    # local time, so compute the epoch offset from 1970-01-01 explicitly.
    epoch_ms = int((expires_at - datetime(1970, 1, 1)).total_seconds() * 1000)
    header = {'alg': 'ES256', 'x5c': ['ZmFrZQ==']}
    payload = {
        'productId': product_id,
        'expiresDate': epoch_ms,
        'environment': environment,
    }
    header_segment = _b64url(json.dumps(header).encode())
    payload_segment = _b64url(json.dumps(payload).encode())
    return f'{header_segment}.{payload_segment}.c2ln'


def _register_entitlement_route(app):
    """Register a throwaway route gated by require_team_entitlement()."""
    name = f'entitlement_route_{uuid_module.uuid4().hex}'
    path = f'/__test__/{name}/'

    def view():
        return require_team_entitlement() or ('ok', 200)

    view.__name__ = name
    handler = auth.login_required(view)
    app.add_url_rule(path, endpoint=name, view_func=handler, methods=['GET'])
    return path


# --- POST /devices/<id>/receipt ---------------------------------------------

def test_receipt_with_active_fake_verifier_returns_active_entitlement(client, app, device_key):
    device_uuid = 'enttest-active-device'
    headers = device_key('enttest-active-key', device_uuid)
    future = datetime.utcnow() + timedelta(days=30)

    app.config['ENTITLEMENT_VERIFIER'] = lambda jws_string: {
        'product_id': TEAM_PRODUCT_ID, 'expires_at': future, 'environment': 'Sandbox'}

    response = client.post(f'/devices/{device_uuid}/receipt', json={'jws': 'whatever'}, headers=headers)
    assert response.status_code == 200
    assert response.get_json()['entitlements']['team']['active'] is True

    get_response = client.get(f'/devices/{device_uuid}/entitlements', headers=headers)
    assert get_response.status_code == 200
    team = get_response.get_json()['team']
    assert team['active'] is True
    assert team['expires_at'] == future.strftime(TIMESTAMP_FORMAT)


def test_receipt_with_expired_fake_verifier_returns_inactive(client, app, device_key):
    device_uuid = 'enttest-expired-device'
    headers = device_key('enttest-expired-key', device_uuid)
    past = datetime.utcnow() - timedelta(days=1)

    app.config['ENTITLEMENT_VERIFIER'] = lambda jws_string: {
        'product_id': TEAM_PRODUCT_ID, 'expires_at': past, 'environment': 'Sandbox'}

    response = client.post(f'/devices/{device_uuid}/receipt', json={'jws': 'x'}, headers=headers)
    assert response.status_code == 200
    assert response.get_json()['entitlements']['team']['active'] is False


def test_receipt_invalid_receipt_returns_400(client, app, device_key):
    device_uuid = 'enttest-invalid-device'
    headers = device_key('enttest-invalid-key', device_uuid)

    def raising_verifier(jws_string):
        raise InvalidReceipt('bad receipt')

    app.config['ENTITLEMENT_VERIFIER'] = raising_verifier

    response = client.post(f'/devices/{device_uuid}/receipt', json={'jws': 'x'}, headers=headers)
    assert response.status_code == 400
    assert response.get_json() == {'reason': 'invalid_receipt'}


def test_receipt_verifier_unavailable_returns_503(client, app, device_key):
    device_uuid = 'enttest-unavailable-device'
    headers = device_key('enttest-unavailable-key', device_uuid)

    def unavailable_verifier(jws_string):
        raise VerifierUnavailable('no verifier configured')

    app.config['ENTITLEMENT_VERIFIER'] = unavailable_verifier

    response = client.post(f'/devices/{device_uuid}/receipt', json={'jws': 'x'}, headers=headers)
    assert response.status_code == 503
    assert response.get_json() == {'reason': 'verifier_unavailable'}


def test_receipt_missing_jws_returns_400(client, device_key):
    device_uuid = 'enttest-missing-jws-device'
    headers = device_key('enttest-missing-jws-key', device_uuid)

    response = client.post(f'/devices/{device_uuid}/receipt', json={}, headers=headers)
    assert response.status_code == 400


# --- require_team_entitlement enforcement -----------------------------------

def test_require_team_entitlement_enforced_denies_without_entitlement(app, client, device_key):
    app.config['ENTITLEMENTS_ENFORCED'] = True
    path = _register_entitlement_route(app)
    headers = device_key('enttest-enforce-key', 'enttest-enforce-device')

    response = client.get(path, headers=headers)
    assert response.status_code == 402
    assert response.get_json() == {'reason': 'entitlement_required'}


def test_require_team_entitlement_enforced_allows_with_active_grant(
        app, client, device_key, grant_entitlement):
    app.config['ENTITLEMENTS_ENFORCED'] = True
    path = _register_entitlement_route(app)
    device_uuid = 'enttest-enforce-granted-device'
    headers = device_key('enttest-enforce-granted-key', device_uuid)
    grant_entitlement(device_uuid, datetime.utcnow() + timedelta(days=1))

    response = client.get(path, headers=headers)
    assert response.status_code == 200


def test_require_team_entitlement_enforced_denies_expired_grant(
        app, client, device_key, grant_entitlement):
    app.config['ENTITLEMENTS_ENFORCED'] = True
    path = _register_entitlement_route(app)
    device_uuid = 'enttest-enforce-expired-device'
    headers = device_key('enttest-enforce-expired-key', device_uuid)
    grant_entitlement(device_uuid, datetime.utcnow() - timedelta(days=1))

    response = client.get(path, headers=headers)
    assert response.status_code == 402


def test_require_team_entitlement_enforced_admin_bypasses(app, client, admin_headers):
    app.config['ENTITLEMENTS_ENFORCED'] = True
    path = _register_entitlement_route(app)

    response = client.get(path, headers=admin_headers)
    assert response.status_code == 200


def test_require_team_entitlement_disabled_allows_without_grant(app, client, device_key):
    app.config['ENTITLEMENTS_ENFORCED'] = False
    path = _register_entitlement_route(app)
    headers = device_key('enttest-disabled-key', 'enttest-disabled-device')

    response = client.get(path, headers=headers)
    assert response.status_code == 200


# --- default_jws_verifier ----------------------------------------------------

def test_default_jws_verifier_with_fake_certificate(app):
    future = datetime.utcnow() + timedelta(days=30)
    jws_string = _build_fake_jws(expires_at=future)

    with app.app_context():
        # Strict mode never trusts sender-supplied certs: with cryptography
        # but no pinned Apple roots configured, and without cryptography at
        # all, the verifier must refuse rather than pseudo-verify.
        app.config['ENTITLEMENT_ALLOW_UNVERIFIED'] = False
        with pytest.raises(VerifierUnavailable):
            default_jws_verifier(jws_string)

        app.config['ENTITLEMENT_ALLOW_UNVERIFIED'] = True
        result = default_jws_verifier(jws_string)
        assert result['product_id'] == TEAM_PRODUCT_ID
        assert abs((result['expires_at'] - future).total_seconds()) < 1


def test_default_jws_verifier_malformed_jws_raises_invalid_receipt(app):
    app.config['ENTITLEMENT_ALLOW_UNVERIFIED'] = True  # bypass availability, isolate parsing
    with app.app_context():
        with pytest.raises(InvalidReceipt):
            default_jws_verifier('only-one-segment.two-segments')


# --- actor rule --------------------------------------------------------------

def test_receipt_actor_rule_forbids_other_device(client, device_key):
    device_a_headers = device_key('enttest-actor-a-key', 'enttest-actor-a')

    response = client.post('/devices/enttest-actor-b/receipt', json={'jws': 'x'},
                            headers=device_a_headers)
    assert response.status_code == 403
    assert response.get_json() == {'reason': 'not_your_device'}


def test_entitlements_get_actor_rule_forbids_other_device(client, device_key):
    device_a_headers = device_key('enttest-actor-get-a-key', 'enttest-actor-get-a')

    response = client.get('/devices/enttest-actor-get-b/entitlements', headers=device_a_headers)
    assert response.status_code == 403
    assert response.get_json() == {'reason': 'not_your_device'}
