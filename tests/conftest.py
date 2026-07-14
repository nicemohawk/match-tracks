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
    flask_app.config['TESTING'] = True
    # Auth/rate-limit knobs honored by match_tracks.auth / match_tracks.rate_limit.
    flask_app.config['API_TOKENS'] = {ADMIN_API_KEY: 'bob'}
    flask_app.config['DEVICE_API_KEYS'] = {}
    flask_app.config['RATE_LIMIT_ENABLED'] = True
    flask_app.config['RATE_LIMIT_WRITE_PER_MINUTE'] = 10000
    flask_app.config['RATE_LIMIT_READ_PER_MINUTE'] = 10000
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
