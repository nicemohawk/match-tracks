"""Tests for the multi-sport plausibility profiles (V2 §4, sports_config)."""

from match_tracks import sports_config
from match_tracks.models import SportProfile

SOCCER_BOUNDS = ((90.0, 130.0), (45.0, 90.0))


def test_ensure_default_sport_profiles_seeds_five_rows(app):
    sports_config.ensure_default_sport_profiles()
    assert SportProfile.objects.count() == 5


def test_ensure_default_sport_profiles_is_idempotent(app):
    sports_config.ensure_default_sport_profiles()
    sports_config.ensure_default_sport_profiles()
    assert SportProfile.objects.count() == 5


def test_plausibility_bounds_for_rugby(app):
    assert sports_config.plausibility_bounds('rugby') == ((94.0, 144.0), (68.0, 70.0))


def test_plausibility_bounds_normalizes_soccer_spellings(app):
    for sport_id in (None, '', 'soccer', 'SOCCER', 'Soccer'):
        assert sports_config.plausibility_bounds(sport_id) == SOCCER_BOUNDS


def test_plausibility_bounds_unknown_sport_falls_back_to_soccer(app):
    assert sports_config.plausibility_bounds('quidditch') == SOCCER_BOUNDS


def test_plausibility_bounds_lazily_seeds_when_empty(app):
    # No explicit ensure_default_sport_profiles() call; the lookup seeds itself.
    assert SportProfile.objects.count() == 0
    assert sports_config.plausibility_bounds('lacrosse') == ((100.0, 110.0), (55.0, 60.0))
    assert SportProfile.objects.count() == 5


def test_operator_edited_profile_survives_reseeding(app):
    sports_config.ensure_default_sport_profiles()

    soccer = SportProfile.objects(sport_id='soccer').first()
    soccer.min_length_m = 42.0
    soccer.max_length_m = 200.0
    soccer.save()

    sports_config.ensure_default_sport_profiles()

    soccer_after = SportProfile.objects(sport_id='soccer').first()
    assert soccer_after.min_length_m == 42.0
    assert soccer_after.max_length_m == 200.0
    # The operator override flows through plausibility_bounds unchanged.
    assert sports_config.plausibility_bounds('soccer') == ((42.0, 200.0), (45.0, 90.0))


def test_normalized_sport_id_truth_table(app):
    assert sports_config.normalized_sport_id(None) is None
    assert sports_config.normalized_sport_id('') is None
    assert sports_config.normalized_sport_id('   ') is None
    assert sports_config.normalized_sport_id('soccer') is None
    assert sports_config.normalized_sport_id('SOCCER') is None
    assert sports_config.normalized_sport_id('rugby') == 'rugby'
    assert sports_config.normalized_sport_id('RUGBY') == 'rugby'
    assert sports_config.normalized_sport_id('  Lacrosse  ') == 'lacrosse'


def test_sports_match_truth_table(app):
    assert sports_config.sports_match(None, 'soccer') is True
    assert sports_config.sports_match(None, '') is True
    assert sports_config.sports_match('soccer', 'SOCCER') is True
    assert sports_config.sports_match('rugby', 'rugby') is True
    assert sports_config.sports_match('rugby', None) is False
    assert sports_config.sports_match('rugby', 'lacrosse') is False


def test_all_profiles_returns_every_sport_profile(app):
    profiles = sports_config.all_profiles()
    assert {profile.sport_id for profile in profiles} == set(
        sports_config.DEFAULT_SPORT_PROFILES)
