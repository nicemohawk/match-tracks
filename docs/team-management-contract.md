# Team Management Contract (proactive team lifecycle)

Frozen decisions (do not change during implementation):
- **Ownership: creator-owned.** First device to create/claim a team owns it; only
  the owner + admin keys may manage (rename/archive/toggle consent). Auto-created
  stubs are *unowned* until claimed.
- **Delete = soft-archive**, reversible, preserves all matches/stats/memberships.
- **Auto-create preserved**: sessions tagging an unknown team still create an
  unowned, unarchived stub (offline-first — no upload friction).

## Model — `Team` (match_tracks/models.py)
Keep existing fields (`code` pk, `name`, `requires_consent`, `created_at`). ADD:
- `owner_device_id = db.StringField(null=True)`  — lowercase device id; `None` = unowned.
- `archived = db.BooleanField(default=False)`
- `archived_at = db.DateTimeField(null=True)`
Add `owner_device_id` to the collection's indexes. Missing fields on legacy docs
read as defaults, so no data backfill is required.

## Auth helper (match_tracks/memberships.py)
- `principal_owns_team(team)` → admin ⇒ True; else `team.owner_device_id is not None
  and effective_device_id() == team.owner_device_id`.
- `owner_denial(team)` → `None`, or `(jsonify({'reason': 'not_team_owner'}), 403)`.
  (404 for an absent team is the caller's job.)

## Endpoints

### `POST /teams` (extend `create_team` in privacy.py) — auth required
Body `{code (required), name?, requires_consent?}`; `code` missing → 400 (keep the
existing `{'reason': ...}` shape). Actor = `effective_device_id()` (may be `None`
for an admin without `X-Device-ID`).
- Team absent → create with `owner_device_id = actor`, name, requires_consent,
  `archived=False`. **201** `{'team': _team_json(team)}`.
- Team exists AND (caller owns it OR it's unowned): update name/requires_consent if
  present; if unowned and actor is a device, adopt ownership (set owner=actor).
  **200** `{'team': ...}`.
- Team exists, owned by someone else, caller not owner/admin → **409**
  `{'reason': 'already_owned'}`.
- Admin owns any team. Admin creating a NEW team with no `X-Device-ID` leaves
  `owner=None` (operator team). The existing test
  `test_team_stats_respect_initials_and_consent` (admin POST /teams → 201) MUST
  still pass.

### `GET /teams/<code>` (new match_tracks/team_admin.py) — auth + membership
404 `{'reason': 'unknown_team'}` if absent; `require_member(code)` (admin bypass)
else 403. Returns:
```
{"team": {"code","name","requires_consent","archived","created_at" (…Z),
          "member_count","is_owner"}}
```
`member_count` = distinct devices across `DeviceTeamMembership(team_code=code)` ∪
`Player(team_code=code)`. `is_owner` = `principal_owns_team(team)`. Do NOT expose the
raw `owner_device_id` (privacy) — only `is_owner`.

### `PATCH /teams/<code>` (team_admin.py) — auth + ownership
Body `{name?, requires_consent?, archived?}`. 404 if absent; `owner_denial` else 403.
Apply provided fields; `archived` True→stamp `archived_at`, False→clear it. Return
**200** `{'team': <same shape as GET>}`.

### `DELETE /teams/<code>` (team_admin.py) — auth + ownership → ARCHIVE
404 if absent; `owner_denial` else 403. Sets `archived=True`, `archived_at=now`
(idempotent — already archived still 200). Keeps matches/memberships/stats. Return
**200** `{'code': code, 'archived': true}`.

### `POST /teams/<code>/claim` (team_admin.py) — auth + membership
404 if absent; `require_member` else 403.
- owner None → set owner = `effective_device_id()`; **200** `{'claimed': true, 'is_owner': true}`.
  (If the caller has no resolvable device id — admin without `X-Device-ID` — 400 `{'reason':'device_required'}`.)
- owner == caller → idempotent **200** `{'claimed': false, 'is_owner': true}`.
- owned by someone else, not admin → **403** `{'reason': 'already_owned'}`.
- admin + `X-Device-ID` → may transfer ownership to that device.

## Changes to existing endpoints
- `join_team` (memberships.py): if the team exists and is **archived** → **409**
  `{'reason': 'team_archived'}` before creating any membership. A join to an
  absent team still auto-creates an unowned, unarchived team.
- `list_teams` (memberships.py): add `"archived"` to each item.
- `_ensure_team` / session ingest (routes.py): UNCHANGED. Sessions tagging an
  archived team are still accepted — archiving never blocks data upload.

## Migration (scripts/migrate_v2.py)
Additive only: `Team.ensure_indexes()` (creates the `owner_device_id` index). No
backfill (defaults cover legacy docs). Must stay idempotent.

## Blueprint wiring
Add `team_admin_blueprint` to `match_tracks/__init__.py` (import + `register_blueprint`)
following the existing V2 blueprint pattern.

## Acceptance tests (tests/test_team_admin.py)
1. Device creates a team → owner set; GET shows `is_owner` true for owner, false for another member.
2. Non-owner PATCH → 403; owner PATCH renames → 200 and GET reflects it.
3. Owner DELETE archives; GET shows `archived` true; team **stats still return**;
   a different device's join → 409 `team_archived`.
4. PATCH `{archived:false}` unarchives; join allowed again.
5. A member claims an auto-created (unowned) team → becomes owner; a second device
   claiming → 403 `already_owned`.
6. POST /teams for a team owned by another device → 409; by the owner → 200 (updates name).
7. Admin manages any team (PATCH/DELETE) regardless of owner.
8. GET /teams/<code>: 404 unknown, 403 non-member, 200 member with correct
   `member_count` (2 devices: one membership + one legacy default).
9. Auto-create intact: a session tagging a brand-new team still 200 and creates an
   unowned, unarchived team.
10. Full existing suite stays green (especially the current POST /teams test).

Constraints: purely additive; do not break V1 legacy endpoints or the existing
202-test suite. Run `ENV=test PYTHONPATH=. .venv/bin/python -m pytest tests/ -q`
and report the exact final line.
