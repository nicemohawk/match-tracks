"""Team event timeline (V2 §11).

A coach view coalesces every player's tagged match events (goals, cards, subs,
flags, notes) into one chronological feed. Events arrive from two sources that
share the same event id:

* ``new_events`` on ``POST /devices/<id>/live`` (§1) during a match — appended
  provisionally to the team-scoped ``MatchEvent`` log (``ingest_live_events``).
* the reconciled ``sessions.events[]`` when the match is uploaded via
  ``/devices/<id>/sessions`` — the authoritative copy, which replaces the
  provisional one keyed by event id while preserving any coach annotation
  (``reconcile_session_events``).

The timeline is the union, deduped by event id. A coach annotates an event's
``coach_label`` via ``POST /matches/<match_uuid>/events/<id>/annotation``,
separate from the player's own ``note``.
"""

from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request
from mongoengine import NotUniqueError

from match_tracks import entitlements, memberships
from match_tracks.auth import auth
from match_tracks.models import MatchEvent, Player, Team
from match_tracks.privacy import player_display_name
from match_tracks.rate_limit import rate_limited

events_blueprint = Blueprint('events', __name__)

TIMESTAMP_FORMAT = '%Y-%m-%dT%H:%M:%SZ'
DEFAULT_SINCE_HOURS = 6


def _parse_timestamp(value):
    """Parse an ISO-8601 timestamp, tolerating a trailing 'Z'. None if empty/bad."""
    if not value:
        return None
    text = str(value).strip()
    if text.endswith('Z'):
        text = text[:-1]
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _format_timestamp(value):
    """Serialize a datetime as ``...Z`` (UTC, second precision), or None."""
    if value is None:
        return None
    return value.strftime(TIMESTAMP_FORMAT)


def _event_id(event):
    """Lowercase event id from a wire event dict (key ``id``), or None."""
    raw = (event or {}).get('id')
    return str(raw).strip().lower() if raw else None


def ingest_live_events(team_code, match_uuid, device_id, new_events):
    """Append live-tagged events to the team log (provisional), deduped by id.

    A re-sent id is a no-op (never a duplicate, never an overwrite): a
    provisional event is kept as first seen until the authoritative upload
    reconciles it. Called only after the live POST's sequence guard passes.
    """
    for event in new_events or []:
        event_id = _event_id(event)
        if not event_id:
            continue
        if MatchEvent.objects(event_uuid=event_id).first() is not None:
            continue
        try:
            MatchEvent(
                event_uuid=event_id,
                match_uuid=match_uuid,
                team_code=team_code,
                device_id=device_id,
                kind=event.get('kind'),
                date=_parse_timestamp(event.get('date')),
                note=event.get('note'),
                source=event.get('source'),
                provisional=True,
            ).save(force_insert=True)
        except NotUniqueError:
            continue  # a concurrent live POST inserted the same id first — fine


def reconcile_session_events(match_uuid, team_code, device_id, events):
    """Reconcile uploaded session events into the timeline (authoritative).

    The uploaded copy replaces the provisional live-log copy of the same id
    (edited notes / manual overrides win) while preserving any coach_label the
    coach already set. Idempotent on re-upload.
    """
    for event in events or []:
        event_id = _event_id(event)
        if not event_id:
            continue
        row = MatchEvent.objects(event_uuid=event_id).first() or MatchEvent(event_uuid=event_id)
        row.match_uuid = match_uuid
        row.team_code = team_code
        row.device_id = device_id
        row.kind = event.get('kind')
        row.date = _parse_timestamp(event.get('date'))
        row.note = event.get('note')
        row.source = event.get('source')
        row.provisional = False
        # coach_label is intentionally left untouched — preserved across reconcile.
        row.save()


# GET the unified team event timeline
@events_blueprint.route('/teams/<code>/events', methods=['GET'])
@auth.login_required
@rate_limited('read')
def get_team_events(code):
    """Every team member's tagged events within the last ``since_hours``
    (default 6), oldest-first. Names respect ``initials_only`` (§5); unknown
    ``kind`` strings pass through verbatim. Membership required; reads stay open
    to any member (no entitlement — a lapsed coach can still review)."""
    member_denial = memberships.require_member(code)
    if member_denial:
        return member_denial

    try:
        since_hours = int(request.args.get('since_hours', DEFAULT_SINCE_HOURS))
    except (TypeError, ValueError):
        since_hours = DEFAULT_SINCE_HOURS
    if since_hours <= 0:
        since_hours = DEFAULT_SINCE_HOURS

    cutoff = datetime.utcnow() - timedelta(hours=since_hours)
    rows = MatchEvent.objects(team_code=code, date__gte=cutoff).order_by('date')

    events = []
    for row in rows:
        player = Player.objects(device_id=row.device_id).first() if row.device_id else None
        item = {
            'id': row.event_uuid,
            # Always a string: the client's TeamEvent.player_name is non-optional,
            # so an authorless event (no Player row) must still carry "".
            'player_name': player_display_name(player) or '',
            'match_uuid': row.match_uuid,
            'kind': row.kind,
            'date': _format_timestamp(row.date),
        }
        if row.note is not None:
            item['note'] = row.note
        if row.coach_label is not None:
            item['coach_label'] = row.coach_label
        if row.source is not None:
            item['source'] = row.source
        events.append(item)

    return jsonify({'events': events})


# POST a coach annotation onto one event
@events_blueprint.route('/matches/<match_uuid>/events/<event_id>/annotation', methods=['POST'])
@auth.login_required
@rate_limited('write')
def annotate_event(match_uuid, event_id):
    """Set an event's ``coach_label`` (separate from the player's note).
    Idempotent overwrite; an empty/absent label clears it. Requires team
    membership AND an active team entitlement (a coach write)."""
    row = MatchEvent.objects(event_uuid=str(event_id).strip().lower()).first()
    if row is None or row.match_uuid != str(match_uuid).strip().lower():
        return jsonify({'reason': 'unknown_event'}), 404

    member_denial = memberships.require_member(row.team_code)
    if member_denial:
        return member_denial

    entitlement_denial = entitlements.require_team_entitlement()
    if entitlement_denial:
        return entitlement_denial

    body = request.get_json(silent=True) or {}
    label = body.get('label')
    row.coach_label = label.strip() if isinstance(label, str) and label.strip() else None
    row.save()

    return '', 204
