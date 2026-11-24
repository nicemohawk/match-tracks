"""Idempotent V3 data migration (captain-led teams).

Additive and safe to re-run. The lazy backfill in ``memberships.ensure_team_
roles`` keeps live reads correct regardless, so this exists to (a) create
``device_teams`` rows for legacy default-team players so they appear in the §2
members list, and (b) stamp ``member_id``/``role``/``current_code`` across the
whole dataset in one pass.

Run with::

    ENV=prod PYTHONPATH=. python -m match_tracks.migrate_v3
"""

import uuid as uuid_module

from mongoengine import NotUniqueError

from match_tracks.models import DeviceTeamMembership, Player, Team


def backfill_current_code():
    """Set ``current_code = code`` on any team missing it."""
    changed = 0
    for team in Team.objects(current_code=None):
        team.current_code = team.code
        team.save()
        changed += 1
    return changed


def backfill_memberships_from_players():
    """Create a device_teams row for each legacy default-team player (V2 §6)."""
    created = 0
    for player in Player.objects(team_code__ne=None):
        if not player.team_code:
            continue
        existing = DeviceTeamMembership.objects(
            device_id=player.device_id, team_code=player.team_code).first()
        if existing is not None:
            continue
        try:
            DeviceTeamMembership(device_id=player.device_id,
                                 team_code=player.team_code,
                                 member_id=str(uuid_module.uuid4())).save()
            created += 1
        except NotUniqueError:
            pass
    return created


def backfill_member_ids_and_captains():
    """Stamp member_id on every row and elect the earliest member as captain."""
    stamped = 0
    for row in DeviceTeamMembership.objects(member_id=None):
        row.member_id = str(uuid_module.uuid4())
        try:
            row.save()
            stamped += 1
        except NotUniqueError:
            row.member_id = str(uuid_module.uuid4())
            row.save()
            stamped += 1

    teams_fixed = 0
    team_codes = DeviceTeamMembership.objects().distinct('team_code')
    for team_code in team_codes:
        active = list(DeviceTeamMembership.objects(
            team_code=team_code, removed_at=None).order_by('joined_at'))
        if not active:
            continue
        captains = [row for row in active if row.role == 'captain']
        if not captains:
            # Earliest by joined_at (the model default orders stably); the first
            # uploader falls out of joined_at order for legacy rows.
            active[0].role = 'captain'
            active[0].save()
            teams_fixed += 1
    return stamped, teams_fixed


def run_v3_migration():
    """Run every backfill step; returns a summary dict. Idempotent."""
    codes = backfill_current_code()
    memberships_created = backfill_memberships_from_players()
    stamped, teams_fixed = backfill_member_ids_and_captains()
    return {
        'current_code_backfilled': codes,
        'memberships_created_from_players': memberships_created,
        'member_ids_stamped': stamped,
        'captains_elected': teams_fixed,
    }


if __name__ == '__main__':
    # Importing the package initializes the mongoengine connection from config.
    import match_tracks  # noqa: F401

    summary = run_v3_migration()
    for key, value in summary.items():
        print(f'{key}: {value}')
