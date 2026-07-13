# match-tracks

A Flask + MongoDB API that stores GPS tracks recorded by the Match Tracks watch/phone app,
plus a crowd-sourced **community field database** and team stat aggregation.

## API overview

Legacy endpoints (unchanged wire format):

- `POST /devices/` / `GET|PUT|DELETE /devices/<uuid>/` — device registry
- `POST /devices/<uuid>/sessions/` — upload GPS sessions (now also accepts optional
  `uuid`, `duration_s`, `field_uuid`, `team_code`, `player_name`, `events`, `stats`
  keys per session; legacy-only payloads behave exactly as before)
- `POST /devices/<uuid>/fields/` — upload trained field outlines (walked touchlines)
- `GET /devices/<uuid>/sessions/`, `GET /devices/<uuid>/fields/`

New endpoints:

- `GET /health` — liveness + DB ping (`200 {"status":"ok"}` / `503 {"status":"degraded"}`)
- `GET /devices/<uuid>/matches` — paginated match history (`limit`, `offset`)
- `GET /devices/<uuid>/matches/<match_uuid>` — full match record (track, events, stats)
- `GET /teams/<code>/stats` — per-player aggregates (matches, minutes, distance,
  sprints, workrate, goals, assists); optional `since`, `limit`, `offset`
- `GET /fields/nearby?lat=..&lon=..&radius_m=..&min_confidence=..` — community fields
  near a location, sorted by distance

### Community field database

Every uploaded field outline and every match GPS track is treated as an *observation*
of a physical pitch. The server fits an oriented rectangle to each observation
(`match_tracks/geometry.py`), de-duplicates by geometric proximity (centers within
40 m, sides within 15 m, long-axis headings within 10°, 180° flips equivalent), and
merges observations into one canonical field per pitch, weighted by observation count
(`match_tracks/field_service.py`). Duplicate uploads become alias rows whose
`merged_into` points at the canonical field, so client-held uuids keep resolving.
Confidence follows `1 - (1 - base) * 0.85^(n-1)` with base 0.7 for trained outlines
and 0.3 for inferred tracks.

### Authentication & scoping

All keys are presented as `Authorization: APIKey <key>`.

- **Admin keys** (`API_TOKENS` config, defaults to the legacy built-in key) may call
  anything.
- **Device keys** (`DEVICE_API_KEYS` config, mapping key → device uuid) authenticate
  a single watch/phone.
- **Team stats scoping choice:** `GET /teams/<code>/stats` requires an admin key or a
  device key whose `Player` profile belongs to that team. A valid key from a device on
  a *different* team gets `403`.

Write endpoints (`/sessions`, `/fields`) are rate-limited per key (default 60/min);
new read endpoints default to 120/min. The limiter is an in-memory token bucket — it
does **not** coordinate across multiple app instances (use a shared store such as
Redis before scaling out).

### Note on the marshmallow 3 baseline fix

The route layer previously used marshmallow 2 style `data, errors = schema.load(...)`
tuple unpacking, which raises under the marshmallow 3 dependency set this repo pins.
Those call sites were fixed to the marshmallow 3 API while keeping response keys and
status codes identical to the intended legacy contract.

## Setup

1. Install mongodb: `brew tap mongodb/brew` then `brew install mongo`. ([full instructions](https://docs.mongodb.com/manual/tutorial/install-mongodb-on-os-x/))

2. Start `mongod`: follow directions at the end of brew's installation based on your usage (as a service, or single shot).

3. Install dependencies: `pipenv sync --dev`

4. Run locally: `pipenv run python run.py`

## Tests

Tests run against an in-memory mongomock database — no mongod needed:

```
ENV=test PYTHONPATH=. pipenv run python -m pytest tests/
```

## To deploy:

match-tracks is configured to deploy via [dokku](https://github.com/dokku/dokku), a Docker container management service.

1. Add dokku remote: `git remote add dokku dokku@<service-url>:match-tracks`

2. Push the repo: `git push dokku`

3. Set any instance environment variables (like API tokens) on the **remote** service in `instance/config.py`

MongoDB is schemaless, so no schema migration is needed for this upgrade: the new
collections (`matches`, `community_fields`, `players`, `teams`) are created on first
write, and legacy embedded data on `devices` is untouched.
