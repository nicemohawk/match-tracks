"""Shared pytest fixtures: Flask test client wired to an in-memory mongomock DB.

Run the suite with:
    ENV=test PYTHONPATH=. .venv/bin/python -m pytest tests/
"""

import mongoengine
import pytest

# Importing the package connects flask-mongoengine to the (possibly absent)
# local mongod; we immediately swap the default connection for mongomock.
from match_tracks import app as flask_app

mongoengine.disconnect_all()
mongoengine.connect('matchdb', host='mongomock://localhost',
                    uuidRepresentation='standard')

ADMIN_API_KEY = 'hi-bob'


@pytest.fixture()
def app():
    # Several suites register throwaway routes via app.add_url_rule at test time
    # (test_auth_rate_limit, test_entitlements, test_memberships). Flask 3.x locks
    # setup methods once the app has served its first request, so clear that flag
    # before each test to keep the dynamic-registration pattern working. Harness
    # only — no effect on request handling or responses.
    flask_app._got_first_request = False
    flask_app.config['TESTING'] = True
    # Auth/rate-limit knobs honored by match_tracks.auth / match_tracks.rate_limit.
    flask_app.config['API_TOKENS'] = {ADMIN_API_KEY: 'bob'}
    flask_app.config['DEVICE_API_KEYS'] = {}
    flask_app.config['RATE_LIMIT_ENABLED'] = True
    flask_app.config['RATE_LIMIT_WRITE_PER_MINUTE'] = 10000
    flask_app.config['RATE_LIMIT_READ_PER_MINUTE'] = 10000
    # V2 knobs (docs/backend-v2-architecture.md).
    flask_app.config['RATE_LIMIT_LIVE_PER_MINUTE'] = 10000
    flask_app.config['RATE_LIMIT_COMMENT_PER_MINUTE'] = 10000
    flask_app.config['ENTITLEMENTS_ENFORCED'] = False
    flask_app.config['ENTITLEMENT_VERIFIER'] = None
    flask_app.config['ENTITLEMENT_ALLOW_UNVERIFIED'] = False
    flask_app.config['IMAGERY_PROVIDER'] = None
    yield flask_app
    # Isolate tests: wipe every collection and any rate-limiter state.
    connection = mongoengine.get_connection()
    connection.drop_database('matchdb')
    try:
        from match_tracks.rate_limit import reset_rate_limiter
        reset_rate_limiter()
    except ImportError:
        pass


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def admin_headers():
    return {'Authorization': f'APIKey {ADMIN_API_KEY}'}


@pytest.fixture()
def device_key(app):
    """Register a device-scoped API key; returns (key, device_uuid, headers)."""

    def register(key, device_uuid):
        app.config['DEVICE_API_KEYS'][key] = device_uuid
        return {'Authorization': f'APIKey {key}'}

    return register


@pytest.fixture()
def membership(app):
    """Create a device→team membership row directly (V2 fixture)."""

    def join(device_uuid, team_code):
        from match_tracks.models import DeviceTeamMembership, Team
        if Team.objects(code=team_code).first() is None:
            Team(code=team_code).save()
        if DeviceTeamMembership.objects(device_id=device_uuid.lower(),
                                        team_code=team_code).first() is None:
            DeviceTeamMembership(device_id=device_uuid.lower(), team_code=team_code).save()

    return join


@pytest.fixture()
def grant_entitlement(app):
    """Grant (or expire) a team entitlement for a device (V2 fixture)."""

    def grant(device_uuid, expires_at):
        from match_tracks.models import Entitlement
        Entitlement.objects(device_id=device_uuid.lower(),
                            product_id='com.nicemohawk.MatchTracker.team.monthly').update_one(
            set__expires_at=expires_at, set__environment='test', upsert=True)

    return grant
