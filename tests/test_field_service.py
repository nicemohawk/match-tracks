"""Tests for the community field merge service.

The numeric geometry (``match_tracks.geometry``) is implemented by a separate
agent and is stubbed here: every geometry function these tests depend on is
monkeypatched on the ``geometry`` module object that ``field_service`` calls
through, so the suite is green regardless of the geometry implementation.
``average_headings_deg`` is replaced by a plain weighted linear mean.
"""

import pytest

from match_tracks import field_service
from match_tracks.geometry import FittedRect
from match_tracks.models import CommunityField


def make_rect(center_lat=10.0, center_lon=20.0, length_m=100.0,
              width_m=60.0, heading_deg=30.0):
    return FittedRect(center_lat=center_lat, center_lon=center_lon,
                      length_m=length_m, width_m=width_m, heading_deg=heading_deg)


def linear_weighted_headings(a_deg, a_weight, b_deg, b_weight):
    return (a_deg * a_weight + b_deg * b_weight) / (a_weight + b_weight)


@pytest.fixture()
def linear_headings(monkeypatch):
    monkeypatch.setattr(field_service.geometry, 'average_headings_deg',
                        linear_weighted_headings)


# compute_confidence ---------------------------------------------------------
def test_compute_confidence_equals_base_at_one_observation():
    assert field_service.compute_confidence(0.7, 1) == pytest.approx(0.7)
    assert field_service.compute_confidence(0.3, 1) == pytest.approx(0.3)


def test_compute_confidence_strictly_increasing():
    previous = field_service.compute_confidence(0.3, 1)
    for observation_count in range(2, 40):
        current = field_service.compute_confidence(0.3, observation_count)
        assert current > previous
        previous = current


def test_compute_confidence_approaches_but_never_reaches_one():
    high = field_service.compute_confidence(0.3, 100)
    assert high < 1.0
    assert high == pytest.approx(1.0, abs=1e-6)


# Acceptance #11: weighted merge moves values by exactly 1/(n+1) of the delta -
def test_acceptance_eleven_ninth_observation_moves_by_one_tenth(app, linear_headings):
    canonical = CommunityField(
        uuid='canon-11',
        rect_center_lat=10.0,
        rect_center_lon=20.0,
        rect_length_m=100.0,
        rect_width_m=60.0,
        rect_heading_deg=30.0,
        observation_count=9,
        has_trained_observation=False,
        source='inferred',
        confidence=field_service.compute_confidence(0.3, 9),
    )
    canonical.save()
    confidence_before = canonical.confidence

    new_rect = make_rect(center_lat=20.0, center_lon=40.0,
                         length_m=200.0, width_m=100.0, heading_deg=30.0)
    merged = field_service.merge_observation_into_field(canonical, new_rect)

    # Moved by exactly 1/10 of each delta.
    assert merged.rect_center_lat == pytest.approx(11.0)   # 10 + (20-10)/10
    assert merged.rect_center_lon == pytest.approx(22.0)   # 20 + (40-20)/10
    assert merged.rect_length_m == pytest.approx(110.0)    # 100 + (200-100)/10
    assert merged.rect_width_m == pytest.approx(64.0)      # 60 + (100-60)/10
    assert merged.observation_count == 10
    assert merged.confidence > confidence_before


# Outline richer-replacement rule -------------------------------------------
def test_outline_replaced_only_when_richer(app, linear_headings):
    canonical = CommunityField(
        uuid='canon-outline',
        rect_center_lat=10.0, rect_center_lon=20.0,
        rect_length_m=100.0, rect_width_m=60.0, rect_heading_deg=30.0,
        observation_count=1,
        outline=[[0, 0], [1, 1], [2, 2]],
    )
    canonical.save()

    richer = [[0, 0], [1, 1], [2, 2], [3, 3], [4, 4]]
    field_service.merge_observation_into_field(canonical, make_rect(), outline=richer)
    assert canonical.outline == richer

    poorer = [[9, 9], [8, 8]]
    field_service.merge_observation_into_field(canonical, make_rect(), outline=poorer)
    assert canonical.outline == richer  # unchanged, poorer outline rejected


# Source upgrade rules -------------------------------------------------------
def test_source_upgrades_inferred_to_trained_to_community(app, linear_headings):
    canonical = CommunityField(
        uuid='canon-source',
        rect_center_lat=10.0, rect_center_lon=20.0,
        rect_length_m=100.0, rect_width_m=60.0, rect_heading_deg=30.0,
        observation_count=1,
        source='inferred',
        has_trained_observation=False,
        contributing_device_ids=['device-1'],
    )
    canonical.save()

    # Same device contributes a trained observation -> 'trained'.
    field_service.merge_observation_into_field(
        canonical, make_rect(), device_id='device-1', observed_trained=True)
    assert canonical.source == 'trained'

    # A second device -> 'community'.
    field_service.merge_observation_into_field(
        canonical, make_rect(), device_id='device-2', observed_trained=False)
    assert canonical.source == 'community'

    # Community is sticky even with a first-device, non-trained observation.
    field_service.merge_observation_into_field(
        canonical, make_rect(), device_id='device-1', observed_trained=False)
    assert canonical.source == 'community'


# Alias stubs and resolution -------------------------------------------------
def test_resolve_follows_alias_and_is_case_insensitive(app):
    canonical = CommunityField(
        uuid='canonical-uuid',
        rect_center_lat=10.0, rect_center_lon=20.0,
        rect_length_m=100.0, rect_width_m=60.0, rect_heading_deg=30.0,
    )
    canonical.save()
    alias = CommunityField(uuid='alias-uuid', merged_into='canonical-uuid')
    alias.save()

    resolved = field_service.resolve_field_uuid('ALIAS-UUID')
    assert resolved is not None
    assert resolved.uuid == 'canonical-uuid'


def test_resolve_unknown_returns_none(app):
    assert field_service.resolve_field_uuid('nope') is None
    assert field_service.resolve_field_uuid(None) is None


def test_resolve_is_loop_safe(app):
    CommunityField(uuid='loop-a', merged_into='loop-b').save()
    CommunityField(uuid='loop-b', merged_into='loop-a').save()
    assert field_service.resolve_field_uuid('loop-a') is None


# ingest_trained_field -------------------------------------------------------
def test_ingest_trained_field_is_idempotent(app, monkeypatch, linear_headings):
    monkeypatch.setattr(field_service.geometry, 'fit_rect_to_coordinates',
                        lambda coordinates: make_rect())
    outline = [[0, 0], [1, 1], [2, 2], [3, 3]]

    first = field_service.ingest_trained_field('device-1', 'Field-ABC', outline)
    assert first is not None
    assert first.uuid == 'field-abc'
    assert first.observation_count == 1

    # Same uuid again (client retry): no-op, count stays put.
    second = field_service.ingest_trained_field('device-1', 'field-abc', outline)
    assert second.uuid == 'field-abc'
    assert second.observation_count == 1
    assert CommunityField.objects.count() == 1


def test_ingest_trained_field_degenerate_outline_stores_nothing(app, monkeypatch):
    monkeypatch.setattr(field_service.geometry, 'fit_rect_to_coordinates',
                        lambda coordinates: None)
    result = field_service.ingest_trained_field('device-1', 'field-x', [[0, 0]])
    assert result is None
    assert CommunityField.objects.count() == 0


def test_ingest_trained_field_matching_creates_alias_stub(app, monkeypatch, linear_headings):
    monkeypatch.setattr(field_service.geometry, 'fit_rect_to_coordinates',
                        lambda coordinates: make_rect())
    monkeypatch.setattr(field_service.geometry, 'rects_match_same_field',
                        lambda a, b: True)
    monkeypatch.setattr(field_service.geometry, 'haversine_distance_m',
                        lambda lat1, lon1, lat2, lon2: 0.0)

    first_outline = [[0, 0], [1, 1], [2, 2]]
    canonical = field_service.ingest_trained_field('device-1', 'first-uuid', first_outline)
    assert canonical.uuid == 'first-uuid'

    # A second trained upload of the same physical field with a different uuid.
    second_outline = [[0, 0], [1, 1], [2, 2], [3, 3]]
    result = field_service.ingest_trained_field('device-2', 'second-uuid', second_outline)
    assert result.uuid == 'first-uuid'          # merged into the original canonical
    assert result.observation_count == 2
    assert result.source == 'community'          # two contributing devices

    # The incoming uuid now resolves to the canonical via its alias stub.
    assert field_service.resolve_field_uuid('second-uuid').uuid == 'first-uuid'


# ingest_track_observation ---------------------------------------------------
def test_ingest_track_resolves_via_alias(app, monkeypatch, linear_headings):
    monkeypatch.setattr(field_service.geometry, 'fit_track_as_field_observation',
                        lambda coordinates: make_rect())
    canonical = CommunityField(
        uuid='track-canon',
        rect_center_lat=10.0, rect_center_lon=20.0,
        rect_length_m=100.0, rect_width_m=60.0, rect_heading_deg=30.0,
        observation_count=1,
    )
    canonical.save()
    CommunityField(uuid='track-alias', merged_into='track-canon').save()

    result = field_service.ingest_track_observation(
        'device-9', [[0, 0]] * 250, field_uuid='track-alias')
    assert result.uuid == 'track-canon'
    assert result.observation_count == 2


def test_ingest_track_creates_inferred_on_no_match(app, monkeypatch, linear_headings):
    monkeypatch.setattr(field_service.geometry, 'fit_track_as_field_observation',
                        lambda coordinates: make_rect())

    result = field_service.ingest_track_observation('device-9', [[0, 0]] * 250)
    assert result is not None
    assert result.source == 'inferred'
    assert result.outline is None
    assert result.has_trained_observation is False
    assert result.observation_count == 1
    assert result.confidence == pytest.approx(field_service.compute_confidence(0.3, 1))
    # Server-generated uuid, lowercase, not one the caller supplied.
    assert result.uuid == result.uuid.lower()
    assert result.contributing_device_ids == ['device-9']


def test_ingest_track_degenerate_rect_modifies_nothing(app, monkeypatch):
    monkeypatch.setattr(field_service.geometry, 'fit_track_as_field_observation',
                        lambda coordinates: None)

    # No field_uuid: nothing stored, returns None.
    assert field_service.ingest_track_observation('device-9', [[0, 0]]) is None
    assert CommunityField.objects.count() == 0

    # Known field_uuid: returns the canonical for association, untouched.
    canonical = CommunityField(
        uuid='untouched',
        rect_center_lat=10.0, rect_center_lon=20.0,
        rect_length_m=100.0, rect_width_m=60.0, rect_heading_deg=30.0,
        observation_count=5,
    )
    canonical.save()
    result = field_service.ingest_track_observation(
        'device-9', [[0, 0]], field_uuid='untouched')
    assert result.uuid == 'untouched'
    assert result.observation_count == 5  # unchanged
