# Backend upgrade V3: captain-led teams

Binding contract for the "captain-led teams" upgrade. Builds on
`docs/BACKEND_UPGRADE_PROMPT.md` (V1) and `docs/backend-v2-architecture.md`
(V2). Same conventions: snake_case JSON, `Authorization: APIKey {key}`,
idempotent writes, **additive** migrations, naive-UTC timestamps serialized as
`...Z` and parsed tolerating a trailing `Z`, plain-marshmallow where schemas are
used. Contract first, disjoint waves, review before merge.

**The rule.** A team works with or without a coach. Whoever starts it is its
captain; everyone who joins sees the team for free. None of this is in the app's
interface yet — the app shows no control until the server has it.

This document records both the requirement and the **design decisions** taken
where the request left a mechanism open. Decisions are flagged **[D]**.

---

## 0. Fixes the shipped app already depends on

- **Membership is idempotent.** `POST /devices/{id}/teams {"team_code"}` and
  `DELETE /devices/{id}/teams/{code}` may arrive late and repeat until a 2xx.
  Joining an unused code creates the team; the first member is its captain (§1).
  A member with no uploads can still read stats, events, live and formation.
- **`X-Device-ID` lenient.** The app now sends it on every request; older builds
  never did. A missing `X-Device-ID` means "membership unknown" → allow the
  read. (Already the behavior of `require_member`/`is_member`: a `None` device
  is simply not denied on reads; §0 keeps it that way.)
- **Live is free.** The team-entitlement gate is REMOVED from
  `POST /devices/{id}/live`. The only writes that keep the gate are the event
  annotation (§11) and match comments (if they ship).
- **Heart rate dropped.** `heart_rate` is dropped on live ingest and never
  returned from `/teams/{code}/live`. (Older builds may still send it; it is
  ignored.)
- **No `device_id` about other players.** `/teams/{code}/stats` replaces
  `device_id` with an opaque per-team `member_id` (uuid) and adds `role`.
  `avg_workrate_score` leaves `/stats` (the app does not show it). No response
  about another player may contain a device id, heart rate, coordinate, route,
  `avg_hr`, `avg_workrate_score` or guardian name.
- **Nameless members.** `player_name` is never `null`; send `"No name"`.

## 1. Identity and roles

- `POST /devices/{id}/secret` → `{"secret": "…"}`, once per device (409
  afterward). The app stores it in the Keychain and sends `X-Device-Secret`.
  **[D]** The secret is a 32-byte URL-safe random token; only its SHA-256 hash
  is stored (`device_secrets`), compared with `hmac.compare_digest`. All §2–§6
  **writes** require it for a device principal.
  **[D]** Admin API keys (operators, tests) bypass the secret check, consistent
  with every other gate in this codebase (`require_member`, `require_team_
  entitlement`, `actor_denial` all bypass admin). Missing header on a device
  principal → `401 {"reason":"device_secret_required"}`; wrong value →
  `403 {"reason":"device_secret_invalid"}`.
- `device_teams` gains `member_id` (uuid, unique), `role`
  (`captain`|`coach`|`player`, default `player`, not null), `removed_at`
  (nullable). A team has exactly one captain while it has members; any number of
  coaches including none. No endpoint requires a coach to exist.

Role matrix:

| action | player | coach | captain |
|---|---|---|---|
| read everything about the team | ✓ | ✓ | ✓ |
| share the invite | ✓ | ✓ | ✓ |
| take/drop the coach role for oneself | ✓ | ✓ | ✓ |
| set formation, who's in | ✗ | ✓ | ✓ |
| rename, remove a player, give/take coach, new code, hand over | ✗ | ✗ | ✓ |

If the captain leaves or deletes their data without handing over, the
longest-standing remaining member (earliest `joined_at`) becomes captain.

**[D] Captain vs. V2 owner.** V2's `Team.owner_device_id` (used by
`team_admin.py` PATCH/DELETE/claim) is a **separate, pre-existing** concept and
is left unchanged. V3 captain lives in `device_teams.role`. The V3 endpoints use
`role`; the V2 team_admin endpoints keep owner semantics. They coexist; the
first member is both the captain (role) and, when created via `POST /teams`, the
owner. This avoids rewriting V2 behavior and its tests.

**[D] Lazy backfill.** Reads that need roles call a helper that, for the team,
assigns a random `member_id` to any active membership lacking one and ensures
exactly one captain (earliest `joined_at`, else first). This makes live data
correct without requiring the batch migration to have run (fully additive).

## 2. The team

`GET /teams/{code}` (members only):

```json
{ "code": "K7QM2P", "name": "Red Dragons", "created_at": "…", "my_member_id": "…",
  "members": [ { "member_id": "…", "player_name": "Ben L", "role": "captain",
                 "joined_at": "…", "matches_shared": 4, "last_shared_at": "…" } ] }
```

Includes members who have shared nothing (`matches_shared: 0`,
`last_shared_at: null`). Supports `ETag` / `If-None-Match` → `304`.
**[D]** The response is a flat object (not wrapped in `team`), exactly as shown;
this **supersedes** the V2 `team_admin` `GET /teams/{code}` shape
(`{"team": {...member_count, is_owner...}}`), which is removed. ETag is the
SHA-256 (hex, quoted, weak-free) of the canonical JSON of the response body.
`player_name` is never null (`"No name"`); initials-only enforced. Removed and
(for others) consent-blocked members are excluded.

`PUT /teams/{code} {"name": "…"}`: 1–60 chars, single line. Captain only, except
the first member may set it while it is still null. `403 {"reason":"not_
captain"}`; invalid name → `400 {"reason":"invalid_name"}`. Requires secret.

`PUT /devices/{id}/player {"player_name": "…"}`: the typed name, applied to every
team at once (name is per-device). Stored exactly as typed. Requires secret +
`actor_denial`.

`member_id` and `role` are added to `/stats`; `member_id` to `/live`, `/events`,
`/formation`. Names are not unique; the app will match by `member_id`.

## 3. Members

- `DELETE /teams/{code}/members/{member_id}` — captain only. Sets `removed_at`.
  The removed device gets `403 {"reason":"removed"}` from team reads and from
  `POST /devices/{id}/teams` for that code; uploads tagging the team get
  `400 {"reason":"not_a_member"}` (§7). Past matches leave the team's totals
  (excluded from `/stats` and `/matches`). Restored with:
- `POST /teams/{code}/members/{member_id}/restore` — captain only (clears
  `removed_at`).
- `POST /teams/{code}/captain {"member_id"}` — captain only; the old captain
  becomes a player. `204`.
- `PUT /teams/{code}/members/{member_id}/role {"role": "coach"|"player"}` —
  captain for anyone, or anyone for themselves. `captain` is refused (`400
  {"reason":"use_handover"}`): use the handover endpoint.
- `POST /teams/{code}/code` → `{"code": "…"}` — captain only: a fresh join code
  after a leak. **[D]** The internal team key (`Team.code`, primary key) never
  changes; a rotation sets `Team.current_code` to the new code and moves the
  prior advertised code to `Team.previous_code` with
  `previous_code_access_until = now + 30 days`. Resolution maps a presented code
  to its team via `current_code`, then `previous_code` while unexpired. The old
  code stops accepting joins immediately; existing members keep read access
  under either code for 30 days. One grace slot: rotating twice within 30 days
  ends the older grace early. Requires secret.

All role changes and removals are written to an `audit_log` collection (who,
whom, action, when) with a TTL index expiring rows after 90 days. **[D]** The
collection is not named in the V3 migration list but is the natural home for the
"log for 90 days" requirement; a TTL index gives the retention for free.

## 4. Formation and who's in

`GET`/`PUT /teams/{code}/lineup` — captain or coach to write, members to read.

```json
{ "updated_at": "…", "updated_by": "Ben L", "name": "4-3-3",
  "slots": [ { "member_id": "…", "name": null, "x": 0.26, "y": 0.14 },
             { "member_id": null, "name": "Pat", "x": 0.8, "y": 0.5 } ] }
```

`x` up the pitch from our goal, `y` left-to-right, both 0–1; ≤ 11 slots; exactly
one of `member_id` / `name` per slot. Last write wins; `If-Match` on
`updated_at` → `412` so two editors don't silently overwrite. `404` until set.
`/formation` (worked out from matches) is unchanged and separate. `updated_by`
honors initials-only. Requires secret to write. Validation failures → `400
{"reason":"invalid_lineup"}`.

`GET`/`PUT /teams/{code}/next`:

```json
{ "kickoff": "…", "title": "Rovers FC, home", "place": "Demo Park",
  "in": [ { "member_id": "…" }, { "name": "Pat" } ], "updated_at": "…" }
```

Same write rules and `If-Match`/`412`. Cleared (treated as absent → `404`) 12 h
after `kickoff`. `place` is free text; no coordinates.

A `name` in either (lineup slot / `next.in`) is a person not on MatchTracker who
has agreed to nothing: kept only while that lineup/next references it, never
indexed, never returned anywhere else.

## 5. Matches the team played together

`GET /teams/{code}/matches?since=&limit=20`, newest first. Groups uploads whose
kickoffs are within two hours (the app's TeamMinutes rule).

```json
{ "matches": [ { "kickoff": "…", "length_minutes": 90,
  "players": [ { "member_id": "…", "player_name": "Ben L", "minutes": 88,
                 "distance_m": 9120, "goals": 1, "assists": 0, "sprints": 14 } ] } ] }
```

**[D]** A group collects matches ordered by `recorded_at` where each is within
two hours of the group's running latest; `kickoff` is the group's earliest
`recorded_at`, `length_minutes` the max `round(duration_s/60)` in the group (or
null). Per player (deduped by device within the group): `minutes` from
`time_on_pitch_s`, `distance_m` from `total_distance_m`, `sprints` from
`sprint_count`, `goals`/`assists` from events. **Null where unknown, never 0.**
No routes, coordinates, heart rate or effort. Members only; removed and
consent-blocked devices excluded. `limit` default 20, max 100.

Live: `x`/`y` in `POST /devices/{id}/live` stay optional; `/teams/{code}/live`
returns `elapsed_s`, `us_goals`, `them_goals` and `member_id`.

## 6. Invites that open anywhere

- `GET /join/{code}`: an HTML page with the team name (from the link's own
  `?team=` query — the server confirms nothing), the code to type, and the App
  Store link. No member names; no confirmation that a code exists.
- `GET /.well-known/apple-app-site-association`:
  `{"applinks":{"details":[{"appIDs":["<TEAM>.com.nicemohawk.MatchTracker"],
  "components":[{"/":"/join/*"}]}]}}`, served with
  `Content-Type: application/json`. **[D]** `<TEAM>` comes from
  `app.config['APPLE_APP_ID_PREFIX']` (default placeholder `TEAMID`).
- Join rate limits: 10/hour per device, 60/hour per IP. **[D]** Enforced on
  `POST /devices/{id}/teams` with a dedicated hourly fixed-window counter
  (`join_rate.py`); the existing per-minute token bucket is per-API-key, which
  is the shared app key, so it cannot distinguish devices or IPs. Over limit →
  `429 {"reason":"rate_limited"}`.

## 7. Privacy, and minors

- Names returned exactly as typed, except `initials_only`, enforced server-side
  in every response here including `updated_by`.
- `requires_consent` teams: until `consent_acknowledged_at` is set, a member is
  left out of `members`, `matches`, `stats`, `live` and `lineup` for others, and
  their uploads are held (not shown). They still see their own data.
- `requires_consent` joins are **pending** until the captain accepts: a join
  creates a `join_requests` row (with its `member_id`) instead of an active
  membership. `GET /teams/{code}/requests` (captain) lists them;
  `POST /teams/{code}/requests/{member_id} {"accept": true}` accepts (creates the
  membership, reusing the `member_id`) or `{"accept": false}` rejects (deletes
  the request). A leaked code must not be enough to watch children play.
  `requires_consent` live omits `x`/`y`.
- `DELETE /devices/{id}` also clears membership, lineup slots, `updated_by`,
  `next` entries and pending requests, deletes the device secret, and hands the
  captaincy over if the deleted device was captain.
- Leaving or being removed stops uploads to the team (`400 {"reason":"not_a_
  member"}`), including queued ones.
- Never returned to teammates: routes, coordinates, `avg_hr`, `heart_rate`,
  device ids, guardian names. `avg_workrate_score` is gone from `/stats`.

## Migration (additive)

- `device_teams`: add `member_id` (backfill random), `role`, `removed_at`. New
  collections `team_lineups`, `team_next`, `join_requests`, `device_secrets`
  (and `audit_log`, see §3 **[D]**). `Team` gains `current_code`,
  `previous_code`, `previous_code_access_until`.
- Backfill captain = the earliest member (`joined_at`, else first upload).
  Backfill `device_teams` from `players.team_code` (V2) first. Teams whose real
  captain is someone else fix it with the handover.
- Old builds send no secret and no `X-Device-ID`: their uploads and reads keep
  working; they simply get none of §2–§6's writes.
- `match_tracks/migrate_v3.py::run_v3_migration()` is idempotent and safe to
  re-run; the lazy backfill (§1 **[D]**) covers correctness regardless.

## Acceptance tests

1. A device that has only joined (no uploads) reads stats, events, live and
   formation, and is in `members`.
2. First joiner of a new code is captain; a second is a player; no coach exists
   and nothing 4xxs for it.
3. Player → `PUT /teams/{code}`: `403 not_captain`. Captain → `200` and every
   member sees the new name.
4. Removed member: `403 removed` on reads, upload (`400 not_a_member`) and
   rejoin; restored member: all work.
5. Captain deletes their data: the longest-standing member is captain.
6. Live POST with no entitlement: `204`. Annotation with no entitlement: `402`.
7. No response about another player contains a device id, heart rate, coordinate
   or route.
8. `initials_only` and `requires_consent` hold in every endpoint here.
9. Two lineup writes from the same `updated_at`: one `204`, one `412`.
10. Joining, leaving, receipt and lineup replays change nothing (idempotent).
