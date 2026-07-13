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

from flask import current_app, request
from flask_httpauth import HTTPTokenAuth

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


def effective_device_id():
    """The device acting in the current request, or None.

    Device keys act as their registered device; an `X-Device-ID` header that
    disagrees with the key's device is rejected (returns None). Admin keys act
    as whatever `X-Device-ID` names (lowercased), or None when absent.
    """
    principal = current_principal()
    if not principal:
        return None

    header_device_id = request.headers.get('X-Device-ID')
    normalized_header = header_device_id.lower() if header_device_id else None

    if principal.get('admin'):
        return normalized_header

    key_device_id = principal.get('device_id')
    if normalized_header and normalized_header != key_device_id:
        return None
    return key_device_id


def principal_may_read_team(principal, team_code):
    """Return True if `principal` is allowed to read `team_code`'s stats.

    Admin keys may read any team. Device keys may read teams they belong to:
    a device_teams membership row (V2) or their Player profile's default team
    (V1 behavior). Unknown devices are denied.
    """
    if not principal:
        return False

    if principal.get('admin'):
        return True

    device_id = principal.get('device_id')
    if not device_id:
        return False

    from match_tracks.memberships import is_member  # avoid import cycle
    return is_member(device_id, team_code)
