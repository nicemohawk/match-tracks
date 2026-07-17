"""Timezone-aware datetime helpers (Option B).

All datetimes in the app are aware UTC internally. These helpers replace the
deprecated ``datetime.utcnow()`` / ``datetime.utcfromtimestamp()`` (removed in a
future Python; source of the 3.13 DeprecationWarnings) and normalize every wire
timestamp to ISO-8601 UTC with a trailing ``Z``.
"""

from datetime import datetime, timezone


def utcnow():
    """Aware UTC 'now' — non-deprecated replacement for datetime.utcnow()."""
    return datetime.now(timezone.utc)


def utcfromtimestamp(seconds):
    """Aware UTC from a POSIX timestamp — replaces datetime.utcfromtimestamp()."""
    return datetime.fromtimestamp(seconds, timezone.utc)


def to_iso_z(value):
    """ISO-8601 UTC with a 'Z' suffix (preserving microseconds). None -> None.

    Accepts naive (assumed UTC) or aware datetimes.
    """
    if value is None:
        return None
    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def parse_iso(value):
    """Parse an ISO-8601 string (trailing 'Z' or offset) to an aware UTC
    datetime. None/empty -> None. A naive input is assumed UTC."""
    if not value:
        return None
    text = str(value).strip().replace('Z', '+00:00')
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
