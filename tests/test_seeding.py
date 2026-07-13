"""Tests for community field seeding from satellite imagery (V2 §8-9).

A ``FakeImageryProvider`` stands in for a real licensed imagery vendor: its
``line_mask`` returns a hand-built grid (one soccer-shaped blob plus isolated
noise cells) and its ``snapshot_png`` returns fixed bytes, so the pitch
detection and imagery-caching logic can be exercised without any real
imagery license.
"""

from datetime import datetime, timedelta

from match_tracks.models import CommunityField, SeedRequest
from match_tracks.imagery import NullImageryProvider
from match_tracks.seeding import run_pending_seed_jobs

FAKE_PNG_BYTES = b'\x89PNG-fake-bytes'


class FakeImageryProvider:
    """A deterministic stand-in for a licensed satellite imagery provider.

    ``line_mask`` returns a grid centered on the request point containing one
    21 x 14 cell True blob (~105 m x 70 m at 5 m/cell — soccer-plausible) plus
    three isolated single-cell True blobs (noise, far too small to be a
    pitch).
    """

    def __init__(self):
        self.snapshot_calls = []

    def line_mask(self, latitude, longitude, radius_m):
        meters_per_cell = 5.0
        row_count, column_count = 40, 40
        grid = [[False] * column_count for _ in range(row_count)]

        # The 21 x 14 soccer-shaped blob.
        for row in range(2, 23):
            for column in range(2, 16):
                grid[row][column] = True

        # Noise: isolated single True cells, nowhere near the blob.
        grid[30][30] = True
        grid[35][5] = True
        grid[5][35] = True

        return {
            'grid': grid,
            'meters_per_cell': meters_per_cell,
            'origin_lat': latitude,
            'origin_lon': longitude,
        }

    def snapshot_png(self, latitude, longitude, span_m):
        self.snapshot_calls.append((latitude, longitude, span_m))
        return FAKE_PNG_BYTES


# POST /fields/seed-request ---------------------------------------------------
def test_seed_request_accepted(client, device_key):
    headers = device_key('device-key-1', 'device-1')
    response = client.post('/fields/seed-request', json={'lat': 40.0, 'lon': -74.0},
                           headers=headers)
    assert response.status_code == 202
    assert 'request_id' in response.get_json()
    assert SeedRequest.objects.count() == 1


def test_seed_request_same_device_within_24h_is_rate_limited(client, device_key):
    headers = device_key('device-key-1', 'device-1')
    first = client.post('/fields/seed-request', json={'lat': 40.0, 'lon': -74.0},
                        headers=headers)
    assert first.status_code == 202

    second = client.post('/fields/seed-request', json={'lat': 41.0, 'lon': -75.0},
                         headers=headers)
    assert second.status_code == 429
    assert second.get_json()['reason'] == 'seed_request_daily_limit'
    assert SeedRequest.objects.count() == 1


def test_seed_request_different_device_same_day_is_accepted(client, device_key):
    first_headers = device_key('device-key-1', 'device-1')
    second_headers = device_key('device-key-2', 'device-2')

    first = client.post('/fields/seed-request', json={'lat': 40.0, 'lon': -74.0},
                        headers=first_headers)
    assert first.status_code == 202

    second = client.post('/fields/seed-request', json={'lat': 40.0, 'lon': -74.0},
                         headers=second_headers)
    assert second.status_code == 202
    assert SeedRequest.objects.count() == 2


def test_seed_request_missing_lat_is_bad_request(client, device_key):
    headers = device_key('device-key-1', 'device-1')
    response = client.post('/fields/seed-request', json={'lon': -74.0}, headers=headers)
    assert response.status_code == 400
    assert response.get_json()['reason'] == 'lat_lon_required'


def test_seed_request_stale_request_older_than_24h_is_accepted(app, client, device_key):
    headers = device_key('device-key-1', 'device-1')
    first = client.post('/fields/seed-request', json={'lat': 40.0, 'lon': -74.0},
                        headers=headers)
    assert first.status_code == 202

    stale_request = SeedRequest.objects.first()
    stale_request.requested_at = datetime.utcnow() - timedelta(hours=25)
    stale_request.save()

    second = client.post('/fields/seed-request', json={'lat': 40.0, 'lon': -74.0},
                         headers=headers)
    assert second.status_code == 202
    assert SeedRequest.objects.count() == 2


# run_pending_seed_jobs --------------------------------------------------------
def test_run_pending_seed_jobs_creates_one_field_and_ignores_noise(app):
    SeedRequest(uuid='seed-1', device_id='device-1', latitude=40.0, longitude=-74.0,
               radius_m=1500.0, status='pending').save()

    result = run_pending_seed_jobs(FakeImageryProvider())

    assert result == {'processed': 1, 'fields_created': 1}
    assert CommunityField.objects.count() == 1

    seeded_field = CommunityField.objects.first()
    assert seeded_field.seeded is True
    assert seeded_field.confidence == 0.25
    assert seeded_field.observation_count == 0
    assert seeded_field.source == 'community'
    assert seeded_field.outline is None

    completed_request = SeedRequest.objects(uuid='seed-1').first()
    assert completed_request.status == 'completed'


def test_run_pending_seed_jobs_skips_duplicate_at_same_venue(app):
    SeedRequest(uuid='seed-1', device_id='device-1', latitude=40.0, longitude=-74.0,
               radius_m=1500.0, status='pending').save()
    run_pending_seed_jobs(FakeImageryProvider())
    assert CommunityField.objects.count() == 1

    SeedRequest(uuid='seed-2', device_id='device-1', latitude=40.0, longitude=-74.0,
               radius_m=1500.0, status='pending').save()
    result = run_pending_seed_jobs(FakeImageryProvider())

    assert result == {'processed': 1, 'fields_created': 0}
    assert CommunityField.objects.count() == 1  # canonical match skipped the duplicate
    assert SeedRequest.objects(uuid='seed-2').first().status == 'completed'


def test_run_pending_seed_jobs_null_provider_fails_requests(app):
    SeedRequest(uuid='seed-1', device_id='device-1', latitude=40.0, longitude=-74.0,
               radius_m=1500.0, status='pending').save()

    result = run_pending_seed_jobs(NullImageryProvider())

    assert result == {'processed': 1, 'fields_created': 0}
    assert CommunityField.objects.count() == 0
    assert SeedRequest.objects(uuid='seed-1').first().status == 'failed'


# GET /fields/<uuid>/imagery ----------------------------------------------------
def _make_canonical_field(uuid='field-1'):
    field = CommunityField(
        uuid=uuid,
        rect_center_lat=40.0, rect_center_lon=-74.0,
        rect_length_m=100.0, rect_width_m=60.0, rect_heading_deg=30.0,
        source='community', observation_count=1, confidence=0.5,
    )
    field.save()
    return field


def test_imagery_first_request_fetches_and_caches(app, client, admin_headers, tmp_path):
    app.config['IMAGERY_PROVIDER'] = FakeImageryProvider()
    app.config['IMAGERY_CACHE_DIR'] = str(tmp_path)
    _make_canonical_field('field-1')

    response = client.get('/fields/field-1/imagery', headers=admin_headers)

    assert response.status_code == 200
    assert response.data == FAKE_PNG_BYTES
    assert response.headers['Cache-Control'] == 'max-age=2592000'
    assert (tmp_path / 'field-1.png').exists()


def test_imagery_second_request_served_from_cache_even_if_provider_swapped(
        app, client, admin_headers, tmp_path):
    app.config['IMAGERY_PROVIDER'] = FakeImageryProvider()
    app.config['IMAGERY_CACHE_DIR'] = str(tmp_path)
    _make_canonical_field('field-1')

    first = client.get('/fields/field-1/imagery', headers=admin_headers)
    assert first.status_code == 200

    app.config['IMAGERY_PROVIDER'] = NullImageryProvider()
    second = client.get('/fields/field-1/imagery', headers=admin_headers)

    assert second.status_code == 200
    assert second.data == FAKE_PNG_BYTES
    assert second.headers['Cache-Control'] == 'max-age=2592000'


def test_imagery_unknown_field_is_404(app, client, admin_headers, tmp_path):
    app.config['IMAGERY_PROVIDER'] = FakeImageryProvider()
    app.config['IMAGERY_CACHE_DIR'] = str(tmp_path)

    response = client.get('/fields/no-such-field/imagery', headers=admin_headers)

    assert response.status_code == 404
    assert response.get_json()['reason'] == 'unknown_field'


def test_imagery_null_provider_empty_cache_is_404(app, client, admin_headers, tmp_path):
    app.config['IMAGERY_PROVIDER'] = NullImageryProvider()
    app.config['IMAGERY_CACHE_DIR'] = str(tmp_path)
    _make_canonical_field('field-2')

    response = client.get('/fields/field-2/imagery', headers=admin_headers)

    assert response.status_code == 404
    assert response.get_json()['reason'] == 'imagery_unavailable'
