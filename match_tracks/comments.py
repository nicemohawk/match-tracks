"""Match comments / team chat (V2 §2).

Comments hang off a match (``MatchComment``) and are visible to that match's
team. Reads are paginated oldest-first and exclude soft-deleted rows; writes
are idempotent by client-supplied id and server-stamp the author name (honoring
the initials-only privacy flag) and post time.
"""

from datetime import datetime

from flask import Blueprint, jsonify, request

from match_tracks import entitlements, memberships
from match_tracks.auth import auth, effective_device_id
from match_tracks.models import Match, MatchComment, Player
from match_tracks.privacy import display_name, player_display_name
from match_tracks.rate_limit import rate_limited

comments_blueprint = Blueprint('comments', __name__)

TIMESTAMP_FORMAT = '%Y-%m-%dT%H:%M:%SZ'
TOMBSTONE_AUTHOR = '[deleted]'
DEFAULT_LIMIT = 100
MAX_LIMIT = 500
MAX_BODY_LENGTH = 1000


def _format_timestamp(value):
    """Serialize a datetime as ``...Z`` (UTC, second precision), or ``None``."""
    if value is None:
        return None
    return value.strftime(TIMESTAMP_FORMAT)


def _comment_json(comment):
    """Wire representation of a single comment (author tombstone-aware)."""
    author = TOMBSTONE_AUTHOR if comment.author_tombstoned else comment.author_name
    return {
        'id': comment.uuid,
        'match_uuid': comment.match_uuid,
        'author': author,
        'body': comment.body,
        'posted_at': _format_timestamp(comment.posted_at),
    }


def _load_match_or_404(match_uuid):
    """Return the Match for a lowercased uuid, or a (response, 404) tuple."""
    match = Match.objects(uuid=str(match_uuid).lower()).first()
    if match is None:
        return None, (jsonify({'reason': 'unknown_match'}), 404)
    return match, None


# LIST comments for a match (oldest first, paginated)
@comments_blueprint.route('/matches/<match_uuid>/comments', methods=['GET'])
@auth.login_required
@rate_limited('read')
def list_comments(match_uuid):
    """Return the match's comments oldest-first, excluding soft-deleted rows.

    ``?after=<comment id>`` returns only comments strictly newer than that
    comment; ``?limit=`` caps the page (default 100, max 500).
    """
    match, error = _load_match_or_404(match_uuid)
    if error:
        return error

    if match.team_code:
        member_denial = memberships.require_member(match.team_code)
        if member_denial:
            return member_denial

    query = MatchComment.objects(
        match_uuid=str(match_uuid).lower(), deleted=False).order_by('posted_at')

    after_id = request.args.get('after')
    if after_id:
        after_comment = MatchComment.objects(uuid=after_id).first()
        if after_comment is None:
            return jsonify({'reason': 'unknown_after'}), 400
        query = query.filter(posted_at__gt=after_comment.posted_at)

    limit = request.args.get('limit', default=DEFAULT_LIMIT, type=int)
    if limit > MAX_LIMIT:
        limit = MAX_LIMIT
    if limit < 0:
        limit = 0

    comments = [_comment_json(comment) for comment in query.limit(limit)]
    return jsonify({'comments': comments})


# POST a new comment (idempotent by id)
@comments_blueprint.route('/matches/<match_uuid>/comments', methods=['POST'])
@auth.login_required
@rate_limited('comment')
def post_comment(match_uuid):
    """Create a comment on a match, idempotently by client-supplied id.

    Replaying an existing id returns the stored comment unchanged (200). The
    author name is resolved from the acting device's Player row (initials
    respected), falling back to the client-supplied ``author`` only when the
    device has no player row. Post time is server-stamped.
    """
    match, error = _load_match_or_404(match_uuid)
    if error:
        return error

    if match.team_code:
        member_denial = memberships.require_member(match.team_code)
        if member_denial:
            return member_denial

    entitlement_denial = entitlements.require_team_entitlement()
    if entitlement_denial:
        return entitlement_denial

    json_data = request.get_json(silent=True) or {}
    comment_id = json_data.get('id')
    if not comment_id:
        return jsonify({'reason': 'id_required'}), 400

    existing = MatchComment.objects(uuid=comment_id).first()
    if existing is not None:
        return jsonify({'comment': _comment_json(existing)}), 200

    body = json_data.get('body')
    if not body or not str(body).strip():
        return jsonify({'reason': 'body_required'}), 400
    if len(body) > MAX_BODY_LENGTH:
        return jsonify({'reason': 'body_too_long'}), 400

    acting_device_id = effective_device_id()
    player = (Player.objects(device_id=acting_device_id).first()
              if acting_device_id else None)
    if player is not None:
        author_name = player_display_name(player)
    else:
        author_name = display_name(json_data.get('author'), False)

    comment = MatchComment(
        uuid=comment_id,
        match_uuid=str(match_uuid).lower(),
        team_code=match.team_code,
        author_device=acting_device_id,
        author_name=author_name,
        body=body,
        posted_at=datetime.utcnow(),
    )
    comment.save()

    return jsonify({'comment': _comment_json(comment)}), 201
