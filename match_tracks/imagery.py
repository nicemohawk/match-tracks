"""Imagery provider interfaces — implemented per docs/backend-v2-architecture.md.

Satellite/aerial imagery is used two ways by the field-seeding pipeline
(``match_tracks.seeding``): as a white-line detection mask used to find
candidate pitch rectangles, and as a snapshot PNG shown to users as visual
confirmation of a field. No imagery license is configured in this codebase —
choosing a real provider (for example, Apple Maps Server API snapshots, or a
licensed Mapbox/Maxar feed) is a human licensing decision, not one this agent
can make; see the PR notes. Until that decision is made, :class:`NullImageryProvider`
is wired in as the default and simply returns no imagery.

Provider protocol
------------------
Any object exposing these two methods can be used as an imagery provider
(passed via ``app.config['IMAGERY_PROVIDER']`` or returned by
:func:`get_imagery_provider`):

``line_mask(latitude, longitude, radius_m) -> Optional[dict]``
    A white-line detection mask covering a square area around the given
    point, or ``None`` when no imagery is available. When present, the dict
    has the shape::

        {
            'grid': [[bool, ...], ...],   # rows of columns
            'meters_per_cell': float,
            'origin_lat': float,
            'origin_lon': float,
        }

    ``grid`` row 0 is the northernmost row of cells; column 0 is the
    westernmost column. Each ``True`` cell marks a detected white line. The
    ground center of cell ``(row, column)`` is:

        latitude  = origin_lat - (row + 0.5) * meters_per_cell / 111195.0
        longitude = origin_lon + (column + 0.5) * meters_per_cell
                    / (111195.0 * cos(radians(origin_lat)))

``snapshot_png(latitude, longitude, span_m) -> Optional[bytes]``
    PNG image bytes for a square snapshot of side ``span_m`` meters centered
    on the given point, or ``None`` when no imagery is available.
"""

import math
from typing import Optional


class NullImageryProvider:
    """Default imagery provider: no imagery license configured.

    Returns ``None`` from both provider methods. The choice of a real
    provider (Apple Maps Server API snapshots vs. a licensed Mapbox/Maxar
    feed) is a human licensing decision — see the PR notes.
    """

    def line_mask(self, latitude: float, longitude: float, radius_m: float) -> Optional[dict]:
        return None

    def snapshot_png(self, latitude: float, longitude: float, span_m: float) -> Optional[bytes]:
        return None


def get_imagery_provider():
    """Return the configured imagery provider, or a :class:`NullImageryProvider`.

    Reads ``current_app.config['IMAGERY_PROVIDER']``; falls back to a fresh
    :class:`NullImageryProvider` instance when unset.
    """
    from flask import current_app

    return current_app.config.get('IMAGERY_PROVIDER') or NullImageryProvider()


def grid_cell_center(origin_lat: float, origin_lon: float, meters_per_cell: float,
                     row: int, column: int) -> tuple:
    """Ground center (latitude, longitude) of grid cell ``(row, column)``.

    Implements the provider protocol's coordinate formulas: row 0 is the
    northernmost row, column 0 is the westernmost column.
    """
    latitude = origin_lat - (row + 0.5) * meters_per_cell / 111195.0
    longitude = origin_lon + (column + 0.5) * meters_per_cell / (
        111195.0 * math.cos(math.radians(origin_lat)))
    return latitude, longitude
