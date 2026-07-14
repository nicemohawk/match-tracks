# Dependency Security Upgrade — drop flask-mongoengine, move to Flask/Werkzeug 3.x

## Motivation
Flask 2.0.1 / Werkzeug 2.0.1 carry known CVEs. The blocker to upgrading is the
pinned, unmaintained **flask-mongoengine 1.0.0**, which imports
`flask.json.JSONEncoder` (removed in Flask 2.3) and caps Flask at <2.3. Replace
flask-mongoengine with a tiny internal shim and move Flask/Werkzeug to the
current 3.x line. Purely internal — **no API/wire/behavior changes**.

## Hard constraints
- **Python 3.9** compatibility required (the config/.venv are 3.9; Flask 3.1 and
  Werkzeug 3.1 support 3.9).
- The full **211-test suite must stay green**, responses byte-for-byte unchanged.
- **Keep pinned** (NOT the security target; moving them expands blast radius):
  `mongoengine==0.23.1`, `pymongo==3.11.4`, `marshmallow==3.12.2`, the
  `marshmallow-mongoengine` git fork commit, `six==1.16.0`, `gunicorn==20.1.0`.
  `flask-httpauth` may float to latest 4.x (test it).
- **Move to 3.x**: `flask` (>=3.1,<4), `werkzeug` (>=3.1,<4), and their companions
  jinja2 / itsdangerous / click / markupsafe / blinker (resolved by the lock).
- **Drop entirely** (unused, or flask-mongoengine-only transitive deps):
  flask-mongoengine, flask-wtf, wtforms, email-validator.

## How the test suite connects (do not break this)
`tests/conftest.py` imports `match_tracks.app` (which calls the connection init),
then `mongoengine.disconnect_all()` and reconnects to `mongomock://`. So the shim
must register the default connection via **`mongoengine.connect(...)`** exactly as
flask-mongoengine did — lazy (pymongo defers), so import never fails when mongod
is absent, and `disconnect_all()` still clears it.

## Coupling to flask-mongoengine (the ONLY usages — verified)
- `from flask_mongoengine import MongoEngine` in `__init__.py` and `models.py`.
- `db = MongoEngine()` (models) / `db = MongoEngine(app)` (init) — the `db.*` proxy
  and connection init.
- 4 × `QuerySet.first_or_404()` in `routes.py` (~lines 209, 220, 247, 408).
- 1 × `jsonify({'device': created_device})` in `routes.py` (~line 203) that relies
  on flask-mongoengine's JSON encoder to serialize a raw mongoengine Document.
- No `.paginate()`. flask-wtf/wtforms/email-validator imported nowhere.

## Files

### NEW `match_tracks/db.py`
```python
"""Minimal MongoEngine integration replacing the unmaintained flask-mongoengine.

The app used exactly three things from flask-mongoengine: the `db` proxy
(db.Document / db.*Field), config-driven connection init, and
QuerySet.first_or_404(). This supplies those directly on top of mongoengine.
"""
import mongoengine
from flask import abort


class _MongoEngine:
    """`db` proxy: db.Document, db.EmbeddedDocument, db.StringField, db.DictField,
    db.ListField, db.EmbeddedDocumentListField, ... all resolve to the mongoengine
    attribute of the same name."""
    def __getattr__(self, name):
        return getattr(mongoengine, name)


db = _MongoEngine()


def init_app(app):
    """Register the default connection from config (MONGODB_DB / MONGODB_HOST,
    same keys flask-mongoengine read). Lazy — import/boot never fails if mongod
    is down."""
    settings = {'db': app.config['MONGODB_DB']}
    host = app.config.get('MONGODB_HOST')
    if host:
        settings['host'] = host
    mongoengine.connect(**settings)


def first_or_404(queryset):
    """First document, or abort(404) — flask-mongoengine parity."""
    document = queryset.first()
    if document is None:
        abort(404)
    return document
```

### `match_tracks/models.py`
Replace `from flask_mongoengine import MongoEngine` + `db = MongoEngine()` with
`from match_tracks.db import db`. Everything else is untouched — every `db.*`
field/document type resolves through the shim.

### `match_tracks/__init__.py`
Replace the flask-mongoengine import and `db = MongoEngine(app)` with:
```python
from match_tracks import db as db_module
...
db_module.init_app(app)
```
placed at the SAME point in the file (after the ENV/instance config is loaded,
before `from . import routes`). Keep the config-load logic and all blueprint
registrations exactly as they are.

### `match_tracks/routes.py`
- Add `from match_tracks.db import first_or_404`.
- Replace the 4 `X.objects(...).first_or_404()` with `first_or_404(X.objects(...))`.
- create_device (~line 203): change `return jsonify({'device': created_device})`
  to `return jsonify({'device': device_schema.dump(created_device)})` (raw-Document
  serialization no longer works without flask-mongoengine's encoder; `dump` is the
  same serializer get_device already uses). If a test pins the create_device body
  shape, reconcile it to the `dump` shape and note it.

### `Pipfile`
- Remove `flask-mongoengine = "*"`.
- `flask = ">=3.1,<4"`, add `werkzeug = ">=3.1,<4"`.
- Pin `mongoengine = "==0.23.1"`, `pymongo = "==3.11.4"`, `marshmallow = "==3.12.2"`.
  Keep the `marshmallow-mongoengine` git line, `six`, `gunicorn`, `flask-httpauth`.
- Add `[dev-packages]`: `pytest = "*"`, `mongomock = "*"`.
- Keep `python_version = "3.9"`.

### `Pipfile.lock` — regenerate properly
- `pip install --user pipenv` (or into a scratch venv), then `pipenv lock` in the
  repo to regenerate the lock from the new Pipfile.
- Confirm the new lock: NO flask-mongoengine / flask-wtf / wtforms /
  email-validator; contains Flask 3.x, Werkzeug 3.x, blinker.

### Rebuild `.venv` and verify
- Rebuild the project venv to match the new lock (e.g.
  `rm -rf .venv && PIPENV_VENV_IN_PROJECT=1 pipenv sync --dev`, or a plain
  `python3 -m venv .venv` + `pip install` of the resolved runtime+dev set). End
  with a working `.venv/bin/python`.
- **Gate 1 — suite**: `ENV=test PYTHONPATH=. .venv/bin/python -m pytest tests/ -q`
  → **211 passed** (or more), zero failures.
- **Gate 2 — boots clean**:
  `ENV=dev PYTHONPATH=. .venv/bin/python -c "import match_tracks; print('ok', match_tracks.app.name)"`
  (must import with no flask-mongoengine).
- **Gate 3 — versions**: print `Flask` and `Werkzeug` versions from the venv and
  confirm both are 3.x.

## Report
Exact final pytest line; resolved Flask/Werkzeug versions; confirmation the lock
dropped flask-mongoengine + the three unused deps; and any test that needed
reconciling (especially the create_device response shape). Do NOT commit or push.
