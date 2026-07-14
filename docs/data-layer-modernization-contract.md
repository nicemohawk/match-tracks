# Data-Layer Modernization — Python 3.13, drop the marshmallow-mongoengine fork

Extends the security branch `chore/claude_cli_flask-werkzeug-security-upgrade`
(PR #8, already on Flask 3.1.3 / Werkzeug 3.1.8 via the match_tracks/db.py shim).
Purely internal — **no API/wire changes**. Everything below must keep the full
suite green **byte-for-byte**.

## Goal
1. **Python 3.9 → 3.13** (3.9 is EOL). Verified: pymongo 4 / mongoengine 0.29 /
   marshmallow 3.20+ / mongomock / gunicorn 23 all work on 3.13.
2. **gunicorn 20.1.0 → >=23** (fixes CVE-2024-1135 request smuggling).
3. **Drop the `marshmallow-mongoengine` git fork** (the structural liability that
   pinned marshmallow and blocked Python 3.12+) — replace with small hand-rolled
   plain-marshmallow schemas.
4. **Bump the data stack**: marshmallow `>=3.20,<4`, mongoengine `>=0.29,<0.30`,
   pymongo `>=4.6,<5`. Drop `six` (only the fork needed it — verify nothing imports it).

## Byte-compat baseline (MUST reproduce exactly)
Captured from the current marshmallow-mongoengine ModelSchema:
- `DeviceSchema().dump(device)` → keys **`{id, name, vendor_identifier, sessions, fields}`**.
  - `id` = `str(device.pk)` (ObjectId hex), **dump-only**.
  - `sessions` / `fields` = lists of the session/field shape below.
- `SessionSchema`/`FieldSchema` dump → keys **`{uuid, track, recorded_at}`**.
  - `uuid` = string, nullable.
  - `track` = the raw dict pass-through, e.g. `{"coordinates": [[lat, lon], ...]}`.
  - `recorded_at` = **naive ISO-8601 with microseconds, NO trailing 'Z'**
    (e.g. `"2026-07-14T12:09:58.558671"`) — marshmallow's default `DateTime` iso
    serialization.
- **`load()` returns mongoengine model INSTANCES, not dicts** (via `@post_load`):
  - `SessionSchema().load(list, many=True, unknown=EXCLUDE)` → list of `Session`.
  - `device_schema.load({...})` → a `Device` instance (with nested Session/Field
    instances if the payload carries `sessions`/`fields`).
  - `load` must tolerate `unknown=EXCLUDE` (extra wire keys like `firmware_version`,
    `uuid` on device payloads) AND must parse `recorded_at` values that DO carry a
    trailing `Z` (legacy clients send `"2026-07-13T18:04:00Z"`).

## Files

### NEW schema definitions (replace ModelSchema in match_tracks/models.py)
Define plain `marshmallow.Schema` subclasses (do NOT import marshmallow_mongoengine):
```python
from marshmallow import Schema, fields, post_load, EXCLUDE

class SessionSchema(Schema):
    class Meta:
        unknown = EXCLUDE
    uuid = fields.String(allow_none=True, load_default=None)
    track = fields.Raw(required=False, load_default=None)
    recorded_at = fields.DateTime(required=False)   # iso; tolerate trailing 'Z' on load
    @post_load
    def _make(self, data, **kw):
        return Session(**data)
```
- `FieldSchema` is identical with `return Field(**data)`.
- `DeviceSchema`: `id = fields.String(dump_only=True, attribute='pk')` (or a
  `@post_dump`/method field that emits `str(obj.pk)`), `name` (String, allow_none),
  `vendor_identifier` (String), `sessions = fields.Nested(SessionSchema, many=True,
  load_default=list)`, `fields = fields.Nested(FieldSchema, many=True,
  load_default=list)`, `Meta.unknown = EXCLUDE`, and `@post_load` → `Device(**data)`.
- **Verify the exact recorded_at format matches the baseline** (naive isoformat with
  microseconds, no Z). If marshmallow's default DateTime emits a different precision
  or a 'Z', adjust with an explicit format so the byte-compat tests
  (`test_legacy_sessions_get_echoes_stored_track`, etc.) pass.
- **`id` field**: `fields.String(dump_only=True, attribute='pk')` may dump the raw
  ObjectId; ensure it becomes `str(pk)`. Use a `fields.Function(lambda o: str(o.pk))`
  or `@post_dump` if needed so `id` is the hex string, matching the baseline.

Keep `db = ...` import from match_tracks.db and all the mongoengine model classes
(Device/Session/Field/Team/... ) EXACTLY as they are — only the three `ModelSchema`
classes change to hand-rolled Schemas.

### match_tracks/routes.py
- `update_device` (~line 230) uses marshmallow-mongoengine's `device_schema.update(device, json_data)`,
  which no longer exists. Reimplement inline: apply provided keys to the existing
  device (e.g. `if 'name' in json_data: device.name = json_data['name']`;
  `if 'vendor_identifier' in json_data: device.vendor_identifier = json_data['vendor_identifier'].lower()`),
  then `device.save()`; keep the response `{'updated_device': device_schema.dump(device)}`
  and the 422-on-ValidationError behavior. Do NOT change other routes' dump/load calls
  (the hand-rolled schemas are drop-in for `.dump()`/`.load()`).
- Leave everything else (auto-provision, concurrency helpers, endpoints) untouched.

### match_tracks/db.py (shim)
- `init_app` uses `mongoengine.connect(...)`. Under pymongo 4, add
  `uuidRepresentation='standard'` to the connect settings (pymongo 4 no longer
  defaults it). Keep it lazy.

### tests/conftest.py
- mongoengine 0.29 removed the `mongomock://` URI. Change
  `mongoengine.connect('matchdb', host='mongomock://localhost', uuidRepresentation='standard')`
  to `import mongomock` + `mongoengine.connect('matchdb', mongo_client_class=mongomock.MongoClient, uuidRepresentation='standard')`.
- Keep the existing `_got_first_request` reset (Flask 3).

### Pipfile
- `python_version = "3.13"`.
- Remove the `marshmallow-mongoengine` git line and `six`.
- `marshmallow = ">=3.20,<4"`, `mongoengine = ">=0.29,<0.30"`, `pymongo = ">=4.6,<5"`,
  `gunicorn = ">=23"`. Keep `flask = ">=3.1,<4"`, `werkzeug = ">=3.1,<4"`,
  `flask-httpauth = "*"`. `[dev-packages]`: pytest, mongomock.

### Pipfile.lock + .venv
- Regenerate the lock with pipenv (on 3.13). Rebuild `.venv` from it (3.13).
- Confirm the lock: NO marshmallow-mongoengine, NO six, NO flask-mongoengine/flask-wtf/
  wtforms/email-validator; present: Flask 3.x, Werkzeug 3.x, marshmallow 3.20+,
  mongoengine 0.29.x, pymongo 4.x, gunicorn 23.x.

## Watch for (pymongo 4 / mongoengine 0.29 behavior changes)
Run the suite and fix any breakage. Likely spots: QuerySet `.count()`
(get_devices), `.distinct()` (team_admin member_count), `.update_one(upsert=True, set__…)`
(memberships/player upserts), `get_connection().admin.command('ping')` (health),
and any DateTime parsing. Do NOT change wire behavior to make a test pass — fix the
call site to the new API while preserving the response.

## Verification gates (all must pass before reporting)
1. `ENV=test PYTHONPATH=. .venv/bin/python -m pytest tests/ -q` → **211 passed**
   (or more), zero failures. Paste the exact final line.
2. Boots clean: `ENV=dev PYTHONPATH=. .venv/bin/python -c "import match_tracks; print('ok')"`.
3. Print resolved versions: Python, Flask, Werkzeug, marshmallow, mongoengine,
   pymongo, gunicorn — confirm Python 3.13 + the target lines.
4. Byte-compat proof: dump a Device with one session + one field and confirm the
   output keys/shape match the baseline above (id hex string, session
   {uuid, track, recorded_at} with naive-iso recorded_at).

## Report
Exact pytest line; resolved versions; confirmation the lock dropped
marshmallow-mongoengine + six; the byte-compat dump sample; every call site you had
to adapt for pymongo 4 / mongoengine 0.29; and any test reconciled. Do NOT commit or push.
