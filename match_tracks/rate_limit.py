"""In-memory, per-API-key token bucket rate limiting for Flask views.

NOTE: This limiter keeps all bucket state in a single process's memory. It
does NOT coordinate across multiple app instances/workers/dynos - if the app
is scaled out horizontally, each instance enforces its own independent
limits. Sharing limits across instances would require a shared store such as
Redis.
"""

import threading
import time
from functools import wraps

from flask import current_app, jsonify, request

DEFAULT_WRITE_LIMIT_PER_MINUTE = 60
DEFAULT_READ_LIMIT_PER_MINUTE = 120
DEFAULT_LIVE_LIMIT_PER_MINUTE = 30  # ~1 update per 2 s per device
DEFAULT_COMMENT_LIMIT_PER_MINUTE = 10

_lock = threading.Lock()
_buckets = {}


class _TokenBucket:
    def __init__(self, capacity):
        self.capacity = capacity
        self.tokens = capacity
        self.last_refill = time.monotonic()

    def _refill(self, refill_rate_per_second):
        now = time.monotonic()
        elapsed = now - self.last_refill
        if elapsed > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * refill_rate_per_second)
            self.last_refill = now

    def consume(self, refill_rate_per_second):
        self._refill(refill_rate_per_second)
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


def _bucket_key(scope):
    auth_header = request.headers.get('Authorization', '')
    parts = auth_header.split(None, 1)
    token = parts[1] if len(parts) == 2 and parts[0] == 'APIKey' else None
    identity = token if token else request.remote_addr
    return (scope, identity)


def _limit_for_scope(scope):
    if scope == 'write':
        return current_app.config.get(
            'RATE_LIMIT_WRITE_PER_MINUTE', DEFAULT_WRITE_LIMIT_PER_MINUTE)
    if scope == 'live':
        return current_app.config.get(
            'RATE_LIMIT_LIVE_PER_MINUTE', DEFAULT_LIVE_LIMIT_PER_MINUTE)
    if scope == 'comment':
        return current_app.config.get(
            'RATE_LIMIT_COMMENT_PER_MINUTE', DEFAULT_COMMENT_LIMIT_PER_MINUTE)
    return current_app.config.get(
        'RATE_LIMIT_READ_PER_MINUTE', DEFAULT_READ_LIMIT_PER_MINUTE)


def rate_limited(scope):
    """Decorator factory enforcing a per-minute request budget for `scope`.

    `scope` should be 'write' or 'read'; each scope/key combination has its
    own independent bucket.
    """

    def decorator(view_function):
        @wraps(view_function)
        def wrapped(*args, **kwargs):
            if not current_app.config.get('RATE_LIMIT_ENABLED', True):
                return view_function(*args, **kwargs)

            per_minute_limit = _limit_for_scope(scope)
            refill_rate_per_second = per_minute_limit / 60.0
            key = _bucket_key(scope)

            with _lock:
                bucket = _buckets.get(key)
                if bucket is None:
                    bucket = _TokenBucket(per_minute_limit)
                    _buckets[key] = bucket
                allowed = bucket.consume(refill_rate_per_second)

            if not allowed:
                return jsonify({'message': 'rate limit exceeded'}), 429

            return view_function(*args, **kwargs)

        return wrapped

    return decorator


def reset_rate_limiter():
    """Clear all bucket state. Intended for use between tests."""
    with _lock:
        _buckets.clear()
