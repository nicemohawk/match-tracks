"""Tests for the idempotent V3 migration (match_tracks/migrate_v3.py)."""

from datetime import datetime

from match_tracks.migrate_v3 import run_v3_migration
from match_tracks.models import DeviceTeamMembership, Player, Team


def test_migration_backfills_codes_memberships_and_captains(app):
    with app.app_context():
        # A pre-V3 team with no current_code.
        Team(code='mig-team', name='Legacy FC').save()
        # A legacy default-team player with no device_teams row.
        Player(device_id='mig-legacy-dev', team_code='mig-team').save()
        # Two explicit memberships lacking member_id/role (earliest first).
        DeviceTeamMembership(device_id='mig-a', team_code='mig-team',
                             joined_at=datetime(2026, 1, 1)).save()
        DeviceTeamMembership(device_id='mig-b', team_code='mig-team',
                             joined_at=datetime(2026, 2, 1)).save()

        summary = run_v3_migration()

        assert Team.objects(code='mig-team').first().current_code == 'mig-team'
        # Legacy player now has a membership row (so it shows in §2 members).
        assert DeviceTeamMembership.objects(
            device_id='mig-legacy-dev', team_code='mig-team').first() is not None
        # Every row has a member_id.
        for row in DeviceTeamMembership.objects(team_code='mig-team'):
            assert row.member_id
        # Exactly one captain, the earliest member.
        captains = list(DeviceTeamMembership.objects(team_code='mig-team', role='captain'))
        assert len(captains) == 1
        assert captains[0].device_id == 'mig-a'
        assert summary['memberships_created_from_players'] >= 1

        # Idempotent: a second run changes nothing material.
        second = run_v3_migration()
        assert second['current_code_backfilled'] == 0
        assert second['memberships_created_from_players'] == 0
        assert len(list(DeviceTeamMembership.objects(
            team_code='mig-team', role='captain'))) == 1
