"""Hourly fixed-window join rate limiting (V3 §6).

A six-character team code is guessable at scale, so joins are capped at 10 per
hour per device and 60 per hour per IP. This is separate from
``match_tracks.rate_limit`` (a per-API-key token bucket): the shipped app sends
one shared API key, so the key cannot distinguish devices or IPs — the device
id and client IP can.

Like the token bucket, state is per-process memory (see that module's note on
horizontal scaling).
"""

import threading
import time

from flask import current_app

DEFAULT_PER_DEVICE_PER_HOUR = 10
DEFAULT_PER_IP_PER_HOUR = 60

_lock = threading.Lock()
_counters = {}  # key -> [window_hour, count]


def _limit(config_key, default):
    return current_app.config.get(config_key, default)


def _peek_over_limit(key, limit, window):
    window_hour, count = _counters.get(key, (window, 0))
    if window_hour != window:
        count = 0
    return count >= limit


def _consume(key, window):
    window_hour, count = _counters.get(key, (window, 0))
    if window_hour != window:
        window_hour, count = window, 0
    _counters[key] = (window_hour, count + 1)


def join_allowed(device_id, ip_address):
    """True if a join from this device and IP is within both hourly budgets.

    Consumes a token from each budget only when BOTH have room, so a rejection
    on one budget never burns the other's allowance.
    """
    if not current_app.config.get('JOIN_RATE_LIMIT_ENABLED', True):
        return True

    device_limit = _limit('JOIN_RATE_LIMIT_PER_DEVICE_PER_HOUR', DEFAULT_PER_DEVICE_PER_HOUR)
    ip_limit = _limit('JOIN_RATE_LIMIT_PER_IP_PER_HOUR', DEFAULT_PER_IP_PER_HOUR)

    device_key = ('device', device_id)
    ip_key = ('ip', ip_address or 'unknown')

    with _lock:
        window = int(time.time() // 3600)
        if _peek_over_limit(device_key, device_limit, window):
            return False
        if _peek_over_limit(ip_key, ip_limit, window):
            return False
        _consume(device_key, window)
        _consume(ip_key, window)
        return True


def reset_join_rate_limiter():
    """Clear all counters. Intended for use between tests."""
    with _lock:
        _counters.clear()
