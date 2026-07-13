# Backend V2 architecture contract (BINDING)

Coordinator-owned. Implementation agents must not change this document or deviate from it.
Stack: Flask 2.0.1 + MongoEngine 0.23.1 (Python 3.9), mongomock test harness
(`ENV=test PYTHONPATH=. .venv/bin/python -m pytest tests/`). All JSON snake_case. All uuids
lowercase strings. Datetimes serialize as `%Y-%m-%dT%H:%M:%SZ` (UTC, second precision); parse
incoming ISO-8601 by stripping a trailing `Z` (`match_tracks.routes._parse_timestamp` exists).

## Module ownership map

| File | Owner |
|---|---|
| `docs/backend-v2-architecture.md`, `match_tracks/models.py`, `match_tracks/__init__.py`, `match_tracks/routes.py`, `match_tracks/field_service.py`, `scripts/migrate_v2.py` | Coordinator |
| `match_tracks/live.py`, `match_tracks/comments.py`, `tests/test_live.py`, `tests/test_comments.py` | Agent A |
| `match_tracks/formation.py`, `match_tracks/sports_config.py`, `tests/test_formation.py`, `tests/test_sports.py` | Agent B |
| `match_tracks/privacy.py`, `match_tracks/memberships.py`, `match_tracks/entitlements.py`, `tests/test_privacy.py`, `tests/test_memberships.py`, `tests/test_entitlements.py` | Agent C |
| `match_tracks/seeding.py`, `match_tracks/imagery.py`, `tests/test_seeding.py` | Agent D |
| `tests/conftest.py` (append fixtures only, coordinator merges), `match_tracks/auth.py`, `match_tracks/rate_limit.py` (small coordinator-approved extensions) | Coordinator |

New endpoint modules are Flask **Blueprints** named `<module>_blueprint`, registered in
`match_tracks/__init__.py` (already stubbed by the coordinator). Do not add routes to
`routes.py` — cross-cutting edits to existing V1 endpoints are coordinator work.

## New / changed documents (models.py — FROZEN, coordinator-authored)

- `LiveStatus` (collection `live_status`, one doc per device, overwritten): `device_id`
  (StringField primary_key), `team_code`, `match_uuid`, `sequence` (Int), `updated_at`
  (DateTime), `elapsed_s`, `heart_rate` (null ok), `distance_m`, `current_speed` (null ok),
  `on_pitch` (Bool), `us_goals`/`them_goals` (Int null), `x`/`y` (Float null, normalized 0–1).
- `MatchComment` (collection `match_comments`): `id` = `uuid` StringField primary_key,
  `match_uuid`, `team_code`, `author_device`, `author_name` (denormalized, initials-respecting
  at post time), `body` (max 1000), `posted_at`, `deleted` (Bool default False),
  `author_tombstoned` (Bool default False).
- `DeviceTeamMembership` (collection `device_teams`): `device_id`, `team_code`, `joined_at`;
  unique compound index (device_id, team_code).
- `Entitlement` (collection `entitlements`): `device_id`, `product_id`, `expires_at`,
  `environment`; unique (device_id, product_id).
- `SportProfile` (collection `sport_profiles`): `sport_id` primary_key, `min_length_m`,
  `max_length_m`, `min_width_m`, `max_width_m`. Seeded by `ensure_default_sport_profiles()`
  in sports_config (idempotent, called at app init and in test fixtures): soccer 90–130 ×
  45–90; lacrosse 100–110 × 55–60; field_hockey 81.9–100.1 × 49.5–60.5 (91×55 ±10%); rugby
  94–144 × 68–70; ultimate 64–110 × 25–37.
- `SeedRequest` (collection `seed_requests`): `id` uuid pk, `device_id`, `latitude`,
  `longitude`, `radius_m`, `requested_at`, `status` (`pending`/`completed`/`failed`).
- Extended existing documents: `Player.initials_only` (Bool default False),
  `Player.consent_acknowledged_at` (DateTime null); `Team.requires_consent` (Bool default
  False); `CommunityField.sport_id` (String null, NULL ≡ soccer), `CommunityField.seeded`
  (Bool default False); `Match.sport_id` (String null); embedded `Session.uuid` /
  `Field.uuid` (String null — enables legacy-embedded idempotency).
- `Match.stats` / events are DictFields — `mean_x`/`mean_y` in stats and `source` on events
  pass through untouched (V2 §10 is free at the storage layer; endpoints must echo them).

## Auth & scoping (coordinator-provided helpers — use, don't reimplement)

- Existing: `Authorization: APIKey <key>`; `match_tracks.auth.current_principal()` →
  `{'key', 'admin', 'device_id'}`.
- NEW `match_tracks.auth.effective_device_id()` → the device acting in this request:
  device keys → their registered device_id (an `X-Device-ID` header that disagrees → None);
  admin keys → the `X-Device-ID` header value (lowercased) or None. Returns lowercase or None.
- NEW `match_tracks.memberships.is_member(device_id, team_code) -> bool` (Agent C) and
  `require_member(team_code)` returning a Flask error response `(jsonify({'reason':
  'not_a_member'}), 403)` or None when OK — used by live/comments/formation/stats reads.
  Admin principals bypass membership checks.
- Entitlements: `match_tracks.entitlements.has_active_team_entitlement(device_id) -> bool`
  (Agent C). Enforced (402 `{"reason": "entitlement_required"}`) on: `POST /devices/<id>/live`
  and `POST /matches/<uuid>/comments`. Reads never require entitlement. Config:
  `ENTITLEMENTS_ENFORCED` (default True; conftest sets False except entitlement tests).
- Privacy: `match_tracks.privacy.display_name(player_or_name, initials_only) -> str` (Agent C)
  — "Ben Lachman" → "B. L." when initials_only. EVERY endpoint returning a player name calls
  it: team stats, live, comments (at post time), formation.

## Rate limiting

`match_tracks.rate_limit.rate_limited(scope)` gains scopes: `'live'` (default 30/min,
config `RATE_LIMIT_LIVE_PER_MINUTE`), `'comment'` (default 10/min,
`RATE_LIMIT_COMMENT_PER_MINUTE`). Coordinator extends `_limit_for_scope`; agents just use it.

## Endpoints (wire-exact)

### §1 Live (Agent A, `live.py`, blueprint `live_blueprint`)
- `POST /devices/<device_id>/live` — auth + entitlement + membership (of `team_code` in body)
  + `rate_limited('live')`. Body per spec. If a LiveStatus exists for this device with the
  SAME match_uuid and stored sequence >= incoming → 200 `{"stale": true}` (no write). Else
  overwrite the device's row entirely; 204 empty body. `timestamp` maps to `updated_at`
  (fallback: server now). New match_uuid always accepted regardless of old sequence.
- `GET /teams/<code>/live` — auth + membership. `{"players": [{"player_name": <display_name
  or null>, "updated_at": ..., "x", "y", "heart_rate", "distance_m", "on_pitch",
  "stale": <updated_at older than 30 s>}]}` for every device with a live row for that team,
  join Player for name + initials_only. (elapsed_s/current_speed/us_goals/them_goals MAY be
  included additively; the keys above are required.) SSE stream endpoint: OUT OF SCOPE.

### §2 Comments (Agent A, `comments.py`, blueprint `comments_blueprint`)
- `GET /matches/<match_uuid>/comments` — auth + membership of the match's team (404 if match
  unknown). Response `{"comments": [{"id", "match_uuid", "author", "body", "posted_at"}]}`
  oldest-first, excluding soft-deleted; `?after=<id>` returns comments strictly after that
  id's posted_at (pagination); `?limit=` default 100.
- `POST /matches/<match_uuid>/comments` — auth + membership + entitlement +
  `rate_limited('comment')`. Body `{"id", "body", "author"?}`. Idempotent by id (replay → 200,
  same comment, no duplicate). Server stamps posted_at; author = Player row's display_name
  (initials-respected) with client `author` only as fallback when no player row. Body > 1000
  chars → 400. 201 on first create with the created comment JSON.

### §3 Formation (Agent B, `formation.py`, blueprint `formation_blueprint`)
- `GET /teams/<code>/formation?window_days=30` — auth + membership. Pipeline: matches for the
  team within window (recorded_at >= now − window_days); group by field_uuid + kickoff ± 2 h
  (grouping only affects which matches qualify — a "game" needs 1+ match); per player
  (device_id) take mean of stats.mean_x / mean_y across qualifying matches (skip matches
  lacking them); require >= 5 distinct players else 404 `{"reason": "insufficient_data"}`.
  Mirror alignment: for each player, consider (x, y) and (1−x, 1−y); choose the orientation
  nearer the current best template assignment (two-pass refinement is fine). Templates
  (normalized, own goal x=0): 4-4-2, 4-3-3, 3-5-2, 4-2-3-1, 3-4-3 (slot coordinate tables in
  formation.py; roles named like "left midfielder"). Assignment: pure-python Hungarian
  algorithm on Euclidean cost over min(len(players), 11) slots (pad templates by nearest
  slots when < 11 players). `confidence = max(0, 1 − mean_assignment_cost / 0.35)`.
  Response: `{"name", "confidence", "slots": [{"player_name" (display_name), "x", "y",
  "role"}]}` where x/y are the player's aligned mean point.

### §4 Multi-sport (Agent B `sports_config.py` + coordinator wiring)
- `sports_config.plausibility_bounds(sport_id) -> ((min_len, max_len), (min_w, max_w))` from
  the SportProfile collection (fallback soccer). `ensure_default_sport_profiles()` idempotent
  seeding. Coordinator wires: sessions/fields ingest passes `sport_id` through to
  Match/CommunityField; `field_service.ingest_track_observation`/`ingest_trained_field` take
  `sport_id=None` and gate on per-sport bounds instead of the geometry constants; matching
  only merges fields of the same sport (NULL ≡ soccer); `/fields/nearby` optional `sport_id`
  filter (NULL rows count as soccer).

### §5 Privacy (Agent C, `privacy.py`, blueprint `privacy_blueprint`)
- `display_name(full_name, initials_only)` helper (also handles None → None).
- `POST /devices/<id>/consent` — auth (device itself or admin). Body `{"guardian_name",
  "acknowledged": true}` → set Player.consent_acknowledged_at = now (create player row if
  missing); 200 `{"acknowledged_at": ...}`. `acknowledged` false/missing → 400.
- Consent gating: team-stats-style listings exclude players whose team has
  `requires_consent=True` and whose consent_acknowledged_at is null. Helper
  `privacy.consent_blocked(player, team) -> bool` used by coordinator in stats and by A in
  live.
- `DELETE /devices/<device_id>` — auth (the device's own key or admin). Synchronous cascade:
  delete Matches(device_id); for each CommunityField with device in contributing_device_ids:
  remove device, decrement observation_count (floor 1), recompute confidence, KEEP geometry;
  tombstone comments (author_tombstoned=True, author_name="[deleted]"); delete LiveStatus,
  DeviceTeamMembership rows, Player row, legacy Device document (embedded sessions/fields go
  with it), Entitlements. Respond 202 `{"deletion_id": <uuid4>}`; log at warning level.
  Retention: backups purge within 30 days (documented, not enforced in code). Never rate-limit.
- Team creation (existing sessions ingest) accepts optional `team_requires_consent` bool? NO —
  requires_consent is set only when a Team row is explicitly created via
  `POST /teams` (NEW, Agent C in privacy.py or memberships.py): body `{"code", "name"?,
  "requires_consent"?}` — auth any key; idempotent by code (existing team → 200 unchanged).

### §6 Multi-team (Agent C, `memberships.py`, blueprint `memberships_blueprint`)
- `POST /devices/<id>/teams` `{"team_code"}` → create membership (auto-create Team stub),
  idempotent; 201/200. `DELETE /devices/<id>/teams/<code>` → 204 (idempotent). `GET
  /devices/<id>/teams` → `{"teams": [{"team_code", "team_name", "joined_at"}]}`. Auth: the
  device's own key or admin.
- Backfill: `Player.team_code` values become membership rows (migration script, coordinator).
  `players.team_code` stays = default team for V1 compat; adding a first membership does NOT
  change it; sessions ingest still upserts player.team_code last-write-wins (V1 behavior).
- Coordinator wires into sessions ingest: session `team_code` not a membership of the
  uploading device → 400 `{"reason": "not_a_member"}` (auto-create membership when the
  device has NO memberships at all — smooth V1→V2 migration for solo users; document).
  Admin-key uploads bypass. V1 auth-scoping (`principal_may_read_team`) now consults
  memberships too (member of team via device_teams OR legacy player.team_code).

### §7 Entitlements (Agent C, `entitlements.py`, blueprint `entitlements_blueprint`)
- `POST /devices/<id>/receipt` `{"jws": "..."}` — verify via the configured verifier:
  `app.config['ENTITLEMENT_VERIFIER']` callable `jws_string -> {'product_id', 'expires_at'
  (datetime), 'environment'}` or raises. Default `default_jws_verifier` decodes the JWS and,
  when the `cryptography` package is importable, verifies the x5c chain to the bundled Apple
  root (certs dir); without `cryptography` it REFUSES (503 `{"reason":
  "verifier_unavailable"}`) unless `ENTITLEMENT_ALLOW_UNVERIFIED=True` (dev only, default
  False). Upsert Entitlement by (device, product). 200 with entitlements summary.
- `GET /devices/<id>/entitlements` → `{"team": {"active": bool, "expires_at": ...}}` where
  active = any entitlement with product_id containing ".team." and expires_at > now.
- `has_active_team_entitlement(device_id)`; enforcement decorator/helper
  `require_team_entitlement()` for A's write endpoints; respects ENTITLEMENTS_ENFORCED.

### §8–9 Seeding + imagery (Agent D, `seeding.py` + `imagery.py`, blueprint `seeding_blueprint`)
- `POST /fields/seed-request` `{"lat", "lon", "radius_m"?=1500}` — auth; per-device 1/day
  (compare last SeedRequest.requested_at; within 24 h → 429 `{"reason":
  "seed_request_daily_limit"}`); create SeedRequest pending; 202 `{"request_id": ...}`.
- `run_pending_seed_jobs(provider=None)` worker (also `scripts/run_seed_jobs.py` CLI):
  provider = `app.config['IMAGERY_PROVIDER']` or `imagery.NullImageryProvider()`. Provider
  protocol: `line_mask(latitude, longitude, radius_m) -> Optional[{'grid': [[bool]],
  'meters_per_cell': float, 'origin_lat': float, 'origin_lon': float}]` (a white-line mask
  grid; row 0 = northernmost) and `snapshot_png(latitude, longitude, span_m) ->
  Optional[bytes]`. NullImageryProvider returns None for both (no license configured —
  licensing decision flagged to the humans; see PR).
- Pitch detection (`seeding.detect_pitch_rectangles(mask) -> [FittedRect-like dicts]`):
  connected components of True cells → component bounding boxes → convert to meters via
  meters_per_cell → per-sport dimension check (soccer bounds via sports_config) → candidate
  rectangles (axis-aligned in grid space; heading from the box's long axis: 0 or 90). Insert
  via field_service as canonical CommunityFields with `source='community'`,
  `confidence=0.25`, `observation_count=0`, `seeded=True`, server uuid; skip when a matching
  canonical field already exists.
- Coordinator wires `/fields/nearby`: seeded fields with observation_count == 0 are returned
  ONLY when the request's min_confidence <= 0.25; once merged with a real observation
  (observation_count >= 1) normal rules apply (merge sets seeded=False).
- `GET /fields/<uuid>/imagery` — auth. Look up cached object (local object store dir
  `instance/imagery/<uuid>.png`; config `IMAGERY_CACHE_DIR`); on miss ask provider
  `snapshot_png` (span = max(field length, width) × 1.5); store + return
  `image/png` with `Cache-Control: max-age=2592000`; 404 when provider unavailable/returns
  None.

### §10 Events metadata — coordinator: events already pass through; verify `source` survives
sessions POST → matches list/detail; goals/assists aggregation counts manual + automatic
(no filter). Test in coordinator's integration tests.

## Idempotency (acceptance 8, coordinator wiring)
- sessions POST: when a session dict carries a uuid that already exists as a Match → skip the
  legacy embedded append for that dict too (embedded Session gains uuid field; dedupe by it).
  Response shape unchanged.
- fields POST: when field uuid already resolves (canonical or alias) → skip embedded append.
- comments POST idempotent by id; live POST sequence-guarded; memberships/consent idempotent.

## Config keys (conftest defaults in parentheses)
`RATE_LIMIT_LIVE_PER_MINUTE` (10000), `RATE_LIMIT_COMMENT_PER_MINUTE` (10000),
`ENTITLEMENTS_ENFORCED` (False in conftest; entitlement tests flip True),
`ENTITLEMENT_VERIFIER` (None → default), `ENTITLEMENT_ALLOW_UNVERIFIED` (False),
`IMAGERY_PROVIDER` (None), `IMAGERY_CACHE_DIR` (tmp path fixture).

## Migration (`scripts/migrate_v2.py`, coordinator)
Idempotent: ensure indexes; backfill DeviceTeamMembership from players.team_code; seed sport
profiles; default new booleans. Mongo is schemaless — no column DDL. Run:
`ENV=prod PYTHONPATH=. python scripts/migrate_v2.py` before deploying V2 code (safe to re-run).

## Testing conventions
Use existing fixtures (`app`, `client`, `admin_headers`, `device_key`). Conftest gains:
`membership(device_uuid, team_code)` helper fixture, `grant_entitlement(device_uuid,
expires_at)` fixture, ENTITLEMENTS_ENFORCED=False default, live/comment rate limits 10000.
Agents append their needs via coordinator. V1 suite (78 tests) must stay green at every wave
boundary.
