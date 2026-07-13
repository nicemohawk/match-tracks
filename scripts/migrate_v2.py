"""Idempotent V2 migration for the match-tracks MongoDB.

MongoDB is schemaless, so there is no column DDL: this script ensures indexes,
backfills device_teams memberships from players.team_code, and seeds the sport
profile config collection. Safe to re-run. Run BEFORE deploying V2 code:

    ENV=prod PYTHONPATH=. python scripts/migrate_v2.py
"""

import sys


def run():
    from match_tracks import app  # noqa: F401  (connects mongoengine)
    from match_tracks.models import (DeviceTeamMembership, Entitlement, LiveStatus,
                                     MatchComment, Player, SeedRequest, Team)
    from match_tracks.sports_config import ensure_default_sport_profiles

    # 1. Indexes for the new collections (mongoengine creates them lazily; force now).
    for document_class in (LiveStatus, MatchComment, DeviceTeamMembership,
                           Entitlement, SeedRequest):
        document_class.ensure_indexes()
    print("indexes ensured")

    # 2. Backfill memberships from each player's legacy default team.
    created_memberships = 0
    for player in Player.objects(team_code__ne=None):
        if not player.team_code:
            continue
        if Team.objects(code=player.team_code).first() is None:
            Team(code=player.team_code).save()
        existing = DeviceTeamMembership.objects(device_id=player.device_id,
                                                team_code=player.team_code).first()
        if existing is None:
            DeviceTeamMembership(device_id=player.device_id,
                                 team_code=player.team_code).save()
            created_memberships += 1
    print(f"memberships backfilled: {created_memberships}")

    # 3. Sport profile config data.
    ensure_default_sport_profiles()
    print("sport profiles seeded")


if __name__ == '__main__':
    sys.exit(run())
