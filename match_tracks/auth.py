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

import hashlib
import hmac
import os

from flask import current_app, jsonify, request
from flask_httpauth import HTTPTokenAuth

# Legacy hardcoded key, kept for backward compatibility with existing
# operator tooling that has not yet been issued a config-managed key.
DEFAULT_API_TOKENS = {'hi-bob': 'bob'}

auth = HTTPTokenAuth(scheme='APIKey')

# One-shot guard so the loud dev-mode warning is logged only once per process.
_dev_mode_warning_emitted = False


def dev_accept_any_key_enabled():
    """True when the "accept any APIKey" local-testing override is active.

    Enabled by the ``DEV_ACCEPT_ANY_API_KEY`` config flag (on by default under
    ``DevelopmentConfig``). Hard-disabled whenever the process runs under the
    production environment (``ENV=prod``) regardless of config, so a stray flag
    in an instance config can never open authentication on a deployed server.
    """
    if os.environ.get('ENV') == 'prod':
        return False
    return bool(current_app.config.get('DEV_ACCEPT_ANY_API_KEY'))


def _warn_dev_mode_once():
    global _dev_mode_warning_emitted
    if _dev_mode_warning_emitted:
        return
    _dev_mode_warning_emitted = True
    try:
        current_app.logger.warning(
            'DEV_ACCEPT_ANY_API_KEY is ON: every "Authorization: APIKey ..." '
            'value is accepted as an admin principal. Local testing only — this '
            'must never run in production.')
    except Exception:
        pass


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

    # Local-testing escape hatch: any otherwise-unknown key is accepted as an
    # admin principal so the watch/iOS app can hit a local server without
    # provisioning keys. Known keys above keep their real identity, so
    # device-scoped testing still works when device keys ARE configured.
    if dev_accept_any_key_enabled():
        _warn_dev_mode_once()
        return {'key': token, 'admin': True, 'device_id': None, 'dev': True}

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


def hash_device_secret(secret):
    """SHA-256 hex of a device secret (V3 §1). The plaintext is never stored."""
    return hashlib.sha256(str(secret).encode('utf-8')).hexdigest()


def require_device_secret(device_id):
    """None when the caller may perform a §2–§6 write as `device_id`; else a
    ``(response, status)`` tuple.

    A device id alone is not a credential (V3 §0/§1): a device principal must
    present the matching ``X-Device-Secret``. Admin keys (operators, tests)
    bypass, consistent with every other gate in this codebase.
    """
    from match_tracks.models import DeviceSecret

    principal = current_principal()
    if principal and principal.get('admin'):
        return None

    presented = request.headers.get('X-Device-Secret')
    if not presented:
        return jsonify({'reason': 'device_secret_required'}), 401

    record = DeviceSecret.objects(device_id=str(device_id).lower()).first()
    if record is None or not hmac.compare_digest(
            record.secret_hash, hash_device_secret(presented)):
        return jsonify({'reason': 'device_secret_invalid'}), 403
    return None


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
