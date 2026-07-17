# Timezone-aware timestamps (Option B)

Make all datetimes **aware UTC** internally, remove the deprecated
`datetime.utcnow()` / `datetime.utcfromtimestamp()` (removed in a future Python;
source of the 520 warnings on 3.13), and **normalize every wire timestamp to
ISO-8601 UTC with a `Z` suffix**. Verified: `tz_aware=True` is honored by
mongomock + mongoengine 0.29 (read-back is UTC-aware), and BSON stores datetimes
as UTC regardless, so **no data migration is needed**.

## The single intended wire change
- Legacy embedded `recorded_at` currently dumps NAIVE with no offset
  (`2026-07-14T12:09:58.558671`). It becomes **`...558671Z`** (same instant, gains
  the explicit `Z`).
- Every OTHER timestamp already serializes as `...Z` via `strftime('%Y-%m-%dT%H:%M:%SZ')`
  and MUST stay byte-identical (seconds precision, `Z`). Do not add microseconds to those.

## New module `match_tracks/timeutils.py`
```python
from datetime import datetime, timezone

def utcnow():
    """Aware UTC 'now' — non-deprecated replacement for datetime.utcnow()."""
    return datetime.now(timezone.utc)

def utcfromtimestamp(seconds):
    """Aware UTC from a POSIX timestamp — replaces datetime.utcfromtimestamp()."""
    return datetime.fromtimestamp(seconds, timezone.utc)

def to_iso_z(value):
    """ISO-8601 UTC with a 'Z' suffix (preserving microseconds). None -> None.
    Accepts naive (assumed UTC) or aware datetimes."""
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
```

## Connection (aware read-back)
- `match_tracks/db.py` `init_app`: add `tz_aware=True` to the `mongoengine.connect(...)`
  settings (alongside the existing `uuidRepresentation='standard'`).
- `tests/conftest.py`: add `tz_aware=True` to the mongomock `connect(...)`.

## Replace every deprecated call (all files)
- `default=datetime.utcnow` → `default=utcnow` on every model field (models.py:
  Team/Player/CommunityField/Match/LiveStatus/MatchComment/DeviceTeamMembership/
  SeedRequest created_at/updated_at/posted_at/joined_at/requested_at).
- `Session.recorded_at` / `Field.recorded_at` `default=datetime.now` → `default=utcnow`
  (fixes a latent naive-LOCAL-time default bug — should be UTC).
- Every inline `datetime.utcnow()` → `utcnow()` (routes, live, entitlements, comments,
  privacy, team_admin, seeding, formation), and `datetime.utcfromtimestamp(...)` →
  `utcfromtimestamp(...)` (entitlements). Import from `match_tracks.timeutils`.

## Serialization
- `_EmbeddedSchema.recorded_at`: replace `fields.DateTime()` with a custom field that
  DUMPS via `to_iso_z` (→ `...Z`) and LOADS via `parse_iso` (→ aware UTC datetime, still
  tolerating trailing `Z` and unknown keys). `@post_load` still returns Session/Field
  instances; `_drop_empty` still applies (recorded_at is always present → kept).
- The V2 formatters (`_format_timestamp` in routes/live/comments and the inline
  `.strftime('%Y-%m-%dT%H:%M:%SZ')` in memberships/team_admin/privacy/entitlements):
  keep the `%Y-%m-%dT%H:%M:%SZ` output. `strftime` on an aware-UTC datetime already
  formats the correct UTC wall-clock, so output stays byte-identical — but make each
  robust by normalizing to UTC first (`value.astimezone(timezone.utc).strftime(...)`)
  so a non-UTC-aware value could never mis-serialize.
- `routes._parse_timestamp`: reuse `timeutils.parse_iso` (aware UTC) — used for match
  recorded_at and live timestamps; downstream stored/compared values must be aware.

## Comparison sites (must all be aware now)
`live.py` (`updated_at < stale_cutoff`), `entitlements.py` (`expires_at > now`,
`newest_expiry` compares) — with `utcnow()` aware and read-back aware (tz_aware),
these are consistent. Verify no naive/aware mix raises `TypeError`.

## Tests
- Update any test that asserts the OLD naive `recorded_at` wire string to expect the
  new `...Z`. (Check `tests/test_legacy_compat.py`, `test_api.py`, `test_v2_integration.py`.)
- Audit test files that build datetimes (`datetime.utcnow() + timedelta`, etc.) and make
  them aware where they're compared in-memory to app values or stored then compared —
  use aware `datetime.now(timezone.utc)` in fixtures to avoid naive/aware `TypeError`.
  Do NOT weaken assertions; fix the datetime construction.

## Verification gates (all before reporting)
1. `ENV=test PYTHONPATH=. .venv/bin/python -m pytest tests/ -q` → 211 passed (or more),
   and the `datetime.utcnow` DeprecationWarning count drops to ~0 (paste the summary line
   showing the reduced warning count).
2. `grep -rn "datetime.utcnow\|utcfromtimestamp" match_tracks/` returns nothing (all
   replaced).
3. Boots clean; and a dump of a Session shows `recorded_at` ending in `Z`, while a V2
   endpoint timestamp (e.g. a team `created_at`) is still `...Z` at seconds precision.

## Report
Exact pytest line + warning count; confirmation no `datetime.utcnow` remains in
match_tracks/; the recorded_at (`...Z`) and a V2 timestamp sample; files changed; tests
reconciled. Do NOT commit or push.
