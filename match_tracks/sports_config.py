"""Sport plausibility profiles (V2 §4).

Each sport carries the plausible min/max length and width (in meters) of its
pitch. These values live in the ``sport_profiles`` collection rather than as
code constants so an operator can tune them in production; the defaults below
are only used to seed missing rows (operator edits always survive re-seeding).

``sport_id`` normalization: the canonical soccer profile is also the fallback
for unknown sports, and ``None``/``''``/``'soccer'`` (any case) all mean soccer.
Stored ``Match``/``CommunityField`` rows use ``None`` to mean soccer, so the
normalized form collapses soccer to ``None`` while other sports become their
lowercased id.
"""

from typing import Dict, List, Optional, Tuple

from match_tracks.models import SportProfile

# Canonical soccer id and the value used for "this is really soccer".
SOCCER_SPORT_ID = 'soccer'

# Default plausible pitch dimensions as
# (min_length_m, max_length_m, min_width_m, max_width_m).
DEFAULT_SPORT_PROFILES: Dict[str, Tuple[float, float, float, float]] = {
    'soccer': (90.0, 130.0, 45.0, 90.0),
    'lacrosse': (100.0, 110.0, 55.0, 60.0),
    'field_hockey': (81.9, 100.1, 49.5, 60.5),
    'rugby': (94.0, 144.0, 68.0, 70.0),
    'ultimate': (64.0, 110.0, 25.0, 37.0),
}

# Soccer bounds as the ((min_len, max_len), (min_w, max_w)) fallback shape.
_soccer_min_length, _soccer_max_length, _soccer_min_width, _soccer_max_width = \
    DEFAULT_SPORT_PROFILES[SOCCER_SPORT_ID]
SOCCER_BOUNDS: Tuple[Tuple[float, float], Tuple[float, float]] = (
    (_soccer_min_length, _soccer_max_length),
    (_soccer_min_width, _soccer_max_width),
)


def ensure_default_sport_profiles() -> None:
    """Insert any missing default sport profiles (idempotent).

    Only sport ids absent from the collection are created, so operator edits to
    existing rows are never overwritten. Safe to call at every app init and in
    test fixtures.
    """
    for sport_id, dimensions in DEFAULT_SPORT_PROFILES.items():
        if SportProfile.objects(sport_id=sport_id).first() is not None:
            continue
        min_length_m, max_length_m, min_width_m, max_width_m = dimensions
        SportProfile(
            sport_id=sport_id,
            min_length_m=min_length_m,
            max_length_m=max_length_m,
            min_width_m=min_width_m,
            max_width_m=max_width_m,
        ).save()


def normalized_sport_id(sport_id: Optional[str]) -> Optional[str]:
    """Collapse the many spellings of "soccer" to ``None``; lowercase the rest.

    ``None``, ``''`` and ``'soccer'`` (case-insensitive) all normalize to
    ``None`` (the stored representation of soccer). Any other sport becomes its
    lowercased id.
    """
    if sport_id is None:
        return None
    trimmed = sport_id.strip()
    if not trimmed:
        return None
    lowercased = trimmed.lower()
    if lowercased == SOCCER_SPORT_ID:
        return None
    return lowercased


def sports_match(first_sport_id: Optional[str], second_sport_id: Optional[str]) -> bool:
    """True when two sport ids refer to the same sport (soccer ≡ None ≡ '')."""
    return normalized_sport_id(first_sport_id) == normalized_sport_id(second_sport_id)


def plausibility_bounds(
    sport_id: Optional[str],
) -> Tuple[Tuple[float, float], Tuple[float, float]]:
    """Plausible pitch bounds for a sport as ((min_len, max_len), (min_w, max_w)).

    Normalizes ``sport_id`` first (``None``/``''``/``'soccer'`` → soccer). Lazily
    seeds the default profiles when the collection is empty. Unknown sports fall
    back to the soccer bounds.
    """
    if SportProfile.objects.first() is None:
        ensure_default_sport_profiles()

    normalized = normalized_sport_id(sport_id)
    lookup_id = normalized if normalized is not None else SOCCER_SPORT_ID

    profile = SportProfile.objects(sport_id=lookup_id).first()
    if profile is None:
        return SOCCER_BOUNDS

    return (
        (profile.min_length_m, profile.max_length_m),
        (profile.min_width_m, profile.max_width_m),
    )


def all_profiles() -> List[SportProfile]:
    """Every SportProfile row (seeding the defaults first when empty)."""
    if SportProfile.objects.first() is None:
        ensure_default_sport_profiles()
    return list(SportProfile.objects)
