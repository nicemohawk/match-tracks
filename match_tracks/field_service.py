"""Community field database: merging field observations into canonical fields.

Devices contribute two kinds of evidence about where a physical pitch is:

* trained field outlines — a player walks the perimeter and uploads the raw
  ``[[lat, lon], ...]`` outline (high-quality geometry), and
* match GPS tracks — the coarse point cloud swept out while playing on the
  pitch (lower-quality, under-covers the field).

Each observation is fit to an oriented rectangle (``geometry.FittedRect``) and
either merged into an existing canonical :class:`~match_tracks.models.CommunityField`
row describing the same physical pitch, or stored as a brand-new canonical row.
When two uploads turn out to describe the same field, the loser becomes an
"alias stub" whose ``merged_into`` points at the surviving canonical uuid, so
that later references by the original field_uuid still resolve.

Confidence formula
------------------
A field's confidence starts from a base that reflects the best evidence seen so
far (``CONFIDENCE_BASE_TRAINED`` if any trained observation contributed,
otherwise ``CONFIDENCE_BASE_INFERRED``) and climbs toward 1.0 as independent
observations accumulate:

    confidence = 1 - (1 - base) * CONFIDENCE_DECAY ** (observation_count - 1)

The remaining gap to 1.0 shrinks by a constant factor (``CONFIDENCE_DECAY``)
with every extra observation, so confidence is monotonically increasing in the
observation count and asymptotically approaches — but never reaches — 1.0.
"""

import uuid as uuid_module
from typing import Optional, Sequence

from match_tracks import geometry
from match_tracks.geometry import FittedRect
from match_tracks.models import CommunityField

# Confidence bases: trained outlines are trusted more than inferred tracks.
CONFIDENCE_BASE_TRAINED = 0.7
CONFIDENCE_BASE_INFERRED = 0.3
# Per-observation shrink factor of the remaining gap to full confidence.
CONFIDENCE_DECAY = 0.85

# Bounding-box prefilter half-width in degrees (~200 m) before the exact
# geometry predicate decides whether two rectangles are the same field.
CENTER_BOUNDING_BOX_DEGREES = 0.002


def compute_confidence(base: float, observation_count: int) -> float:
    """Confidence for a field seen ``observation_count`` times from ``base``.

    ``1 - (1 - base) * CONFIDENCE_DECAY ** (observation_count - 1)``. With one
    observation the result equals ``base``; each additional observation shrinks
    the gap to 1.0 by ``CONFIDENCE_DECAY``, so the value strictly increases in
    ``observation_count`` and approaches (never reaches) 1.0.
    """
    return 1.0 - (1.0 - base) * CONFIDENCE_DECAY ** (observation_count - 1)


def resolve_field_uuid(field_uuid: Optional[str]) -> Optional[CommunityField]:
    """Return the canonical CommunityField for ``field_uuid``, or ``None``.

    Lowercases the uuid, loads the row, and follows ``merged_into`` alias
    pointers to the surviving canonical row. A ``visited`` set guards against a
    cycle of alias pointers.
    """
    if not field_uuid:
        return None
    current_uuid = field_uuid.lower()
    visited = set()
    while current_uuid and current_uuid not in visited:
        visited.add(current_uuid)
        field = CommunityField.objects(uuid=current_uuid).first()
        if field is None:
            return None
        if field.merged_into is None:
            return field
        current_uuid = field.merged_into.lower()
    return None


def find_matching_canonical_field(rect: FittedRect) -> Optional[CommunityField]:
    """Find the canonical field describing the same physical pitch as ``rect``.

    Prefilters canonical rows (``merged_into=None``) by a small bounding box
    around the rectangle center, then applies ``geometry.rects_match_same_field``.
    If several rows match, returns the one whose center is nearest by haversine
    distance.
    """
    candidates = CommunityField.objects(
        merged_into=None,
        rect_center_lat__gte=rect.center_lat - CENTER_BOUNDING_BOX_DEGREES,
        rect_center_lat__lte=rect.center_lat + CENTER_BOUNDING_BOX_DEGREES,
        rect_center_lon__gte=rect.center_lon - CENTER_BOUNDING_BOX_DEGREES,
        rect_center_lon__lte=rect.center_lon + CENTER_BOUNDING_BOX_DEGREES,
    )

    best_field = None
    best_distance = None
    for candidate in candidates:
        candidate_rect = _rect_from_field(candidate)
        if not geometry.rects_match_same_field(candidate_rect, rect):
            continue
        distance = geometry.haversine_distance_m(
            candidate.rect_center_lat, candidate.rect_center_lon,
            rect.center_lat, rect.center_lon,
        )
        if best_distance is None or distance < best_distance:
            best_field = candidate
            best_distance = distance
    return best_field


def merge_observation_into_field(canonical: CommunityField, rect: FittedRect, *,
                                 outline: Optional[Sequence] = None,
                                 device_id: Optional[str] = None,
                                 observed_trained: bool = False) -> CommunityField:
    """Fold a new observation into ``canonical`` and persist it.

    Center lat/lon, length, and width are updated as an incremental weighted
    average with weights ``(observation_count, 1)``, so a new observation moves
    each toward its value by ``1 / (observation_count + 1)`` of the delta.
    Heading uses ``geometry.average_headings_deg`` (wrap-correct). A richer
    outline replaces a poorer one; contributing devices, trained-evidence flag,
    source, confidence, and observation count are updated per the merge rules.
    """
    existing_count = canonical.observation_count or 0

    canonical.rect_center_lat = _weighted_average(
        canonical.rect_center_lat, existing_count, rect.center_lat)
    canonical.rect_center_lon = _weighted_average(
        canonical.rect_center_lon, existing_count, rect.center_lon)
    canonical.rect_length_m = _weighted_average(
        canonical.rect_length_m, existing_count, rect.length_m)
    canonical.rect_width_m = _weighted_average(
        canonical.rect_width_m, existing_count, rect.width_m)
    canonical.rect_heading_deg = geometry.average_headings_deg(
        canonical.rect_heading_deg, existing_count, rect.heading_deg, 1)

    canonical.observation_count = existing_count + 1

    # Outline: keep whichever has the most perimeter points.
    if outline is not None:
        stored_outline = canonical.outline or []
        if len(outline) > len(stored_outline):
            canonical.outline = list(outline)

    if device_id is not None and device_id not in canonical.contributing_device_ids:
        canonical.contributing_device_ids.append(device_id)

    canonical.has_trained_observation = canonical.has_trained_observation or observed_trained

    # Source upgrade: multiple contributors make it community-sourced;
    # otherwise a trained observation beats an inferred-only field.
    if len(set(canonical.contributing_device_ids)) > 1:
        canonical.source = 'community'
    elif canonical.has_trained_observation:
        canonical.source = 'trained'
    else:
        canonical.source = 'inferred'

    confidence_base = (CONFIDENCE_BASE_TRAINED if canonical.has_trained_observation
                       else CONFIDENCE_BASE_INFERRED)
    canonical.confidence = compute_confidence(confidence_base, canonical.observation_count)

    canonical.save()
    return canonical


def ingest_trained_field(device_id: str, field_uuid: str,
                         outline_coordinates: Sequence[Sequence[float]],
                         name: Optional[str] = None) -> Optional[CommunityField]:
    """Ingest a device-uploaded trained field outline.

    Idempotent on ``field_uuid``: if the uuid already resolves (canonical or
    alias), returns the canonical without bumping counts (client retry safety).
    Otherwise fits a rectangle to the outline (returning ``None`` on a
    degenerate outline — caller treats this as non-fatal). A matching canonical
    field absorbs the observation and an alias stub is created for the incoming
    uuid; with no match a new canonical field is stored.
    """
    normalized_uuid = field_uuid.lower()

    existing = resolve_field_uuid(normalized_uuid)
    if existing is not None:
        return existing

    rect = geometry.fit_rect_to_coordinates(outline_coordinates)
    if rect is None:
        return None

    canonical = find_matching_canonical_field(rect)
    if canonical is not None:
        merge_observation_into_field(
            canonical, rect,
            outline=outline_coordinates,
            device_id=device_id,
            observed_trained=True,
        )
        # Alias stub so later references by the incoming uuid resolve.
        alias = CommunityField(
            uuid=normalized_uuid,
            device_id=device_id,
            name=name,
            merged_into=canonical.uuid,
        )
        alias.save()
        return canonical

    new_field = CommunityField(
        uuid=normalized_uuid,
        device_id=device_id,
        name=name,
        outline=list(outline_coordinates),
        rect_center_lat=rect.center_lat,
        rect_center_lon=rect.center_lon,
        rect_length_m=rect.length_m,
        rect_width_m=rect.width_m,
        rect_heading_deg=rect.heading_deg,
        source='trained',
        observation_count=1,
        confidence=compute_confidence(CONFIDENCE_BASE_TRAINED, 1),
        has_trained_observation=True,
        contributing_device_ids=[device_id],
    )
    new_field.save()
    return new_field


def ingest_track_observation(device_id: str,
                             track_coordinates: Sequence[Sequence[float]],
                             field_uuid: Optional[str] = None) -> Optional[CommunityField]:
    """Ingest a match GPS track as evidence of field geometry.

    Fits the track to a rectangle. A degenerate track yields no geometry: if a
    known ``field_uuid`` was supplied the resolved canonical is returned
    unmodified (for match association), otherwise ``None``. With a usable
    rectangle the observation is merged into the resolved field (if any), else
    into a matching canonical field, else a new server-uuid inferred canonical
    field is created.
    """
    rect = geometry.fit_track_as_field_observation(track_coordinates)
    if rect is None:
        return resolve_field_uuid(field_uuid)

    resolved = resolve_field_uuid(field_uuid)
    if resolved is not None:
        return merge_observation_into_field(
            resolved, rect, outline=None, device_id=device_id, observed_trained=False)

    canonical = find_matching_canonical_field(rect)
    if canonical is not None:
        return merge_observation_into_field(
            canonical, rect, outline=None, device_id=device_id, observed_trained=False)

    new_field = CommunityField(
        uuid=str(uuid_module.uuid4()).lower(),
        device_id=device_id,
        name=None,
        outline=None,
        rect_center_lat=rect.center_lat,
        rect_center_lon=rect.center_lon,
        rect_length_m=rect.length_m,
        rect_width_m=rect.width_m,
        rect_heading_deg=rect.heading_deg,
        source='inferred',
        observation_count=1,
        confidence=compute_confidence(CONFIDENCE_BASE_INFERRED, 1),
        has_trained_observation=False,
        contributing_device_ids=[device_id],
    )
    new_field.save()
    return new_field


def _weighted_average(existing_value: float, existing_count: int, new_value: float) -> float:
    """Incremental weighted mean: ``(existing * count + new) / (count + 1)``."""
    return (existing_value * existing_count + new_value) / (existing_count + 1)


def _rect_from_field(field: CommunityField) -> FittedRect:
    """Reconstruct a :class:`FittedRect` from a stored CommunityField row."""
    return FittedRect(
        center_lat=field.rect_center_lat,
        center_lon=field.rect_center_lon,
        length_m=field.rect_length_m,
        width_m=field.rect_width_m,
        heading_deg=field.rect_heading_deg,
    )
