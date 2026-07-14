"""Authentication and team-scoping for the Match Tracks API.

Two kinds of API keys are accepted, both presented as an HTTP
``Authorization: APIKey {key}`` header:

* Admin keys (``current_app.config['API_TOKENS']``, a dict of key -> label)
  act on behalf of an operator and are not restricted to any single team.
* Device keys (``current_app.config['DEVICE_API_KEYS']``, a dict of
  key -> lowercase device uuid) act on behalf of a single device/watch.

Team-scoping choice: reading a team's stats requires either an admin key,
or a device key whose associated ``Player`` document (see
``match_tracks.models.Player``) has a ``team_code`` that exactly matches the
requested team. Devices with no registered player, or whose player is on a
different team, are denied. This keeps a device from reading another team's
match data just because it knows (or guesses) that team's code.
"""

from flask import current_app
from flask_httpauth import HTTPTokenAuth

from match_tracks.models import Player

# Legacy hardcoded key, kept for backward compatibility with existing
# operator tooling that has not yet been issued a config-managed key.
DEFAULT_API_TOKENS = {'hi-bob': 'bob'}

auth = HTTPTokenAuth(scheme='APIKey')


@auth.verify_token
def verify_token(token):
    if not token:
        return None

    admin_tokens = current_app.config.get('API_TOKENS', DEFAULT_API_TOKENS)
    if token in admin_tokens:
        return {'key': token, 'admin': True, 'device_id': None}

    device_keys = current_app.config.get('DEVICE_API_KEYS', {})
    if token in device_keys:
        return {'key': token, 'admin': False, 'device_id': device_keys[token]}

    return None


def current_principal():
    """Return the principal dict for the currently authenticated request."""
    return auth.current_user()


def principal_may_read_team(principal, team_code):
    """Return True if `principal` is allowed to read `team_code`'s stats.

    Admin keys may read any team. Device keys may only read the team their
    registered Player profile belongs to; unknown devices or devices with no
    matching Player row are denied.
    """
    if not principal:
        return False

    if principal.get('admin'):
        return True

    device_id = principal.get('device_id')
    if not device_id:
        return False

    player = Player.objects(device_id=device_id).first()
    if player is None:
        return False

    return player.team_code == team_code
