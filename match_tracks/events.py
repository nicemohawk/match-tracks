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
# Clock-skew margin when scoping which provisional live events a match upload
# supersedes (device + time; the live match_uuid is unreliable — see §11 notes).
RECONCILE_WINDOW_MARGIN = timedelta(minutes=5)


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


def _apply_live_fields(row, event):
    row.kind = event.get('kind')
    row.date = _parse_timestamp(event.get('date'))
    row.note = event.get('note')
    row.source = event.get('source')


def ingest_live_events(team_code, match_uuid, device_id, new_events):
    """Upsert live-tagged events into the team log (provisional), latest-wins.

    The watch may re-send an event with the SAME id and a corrected kind/note
    (e.g. goalFor -> goalMine after "who scored?"), so a re-send UPDATES the
    provisional row rather than being ignored. ``coach_label`` is never touched.
    An event already reconciled from an authoritative upload (provisional=False)
    is left alone — the upload wins. Called only after the live POST's sequence
    guard passes.
    """
    for event in new_events or []:
        event_id = _event_id(event)
        if not event_id:
            continue
        row = MatchEvent.objects(event_uuid=event_id).first()
        if row is not None and not row.provisional:
            continue  # authoritative upload already won
        if row is None:
            row = MatchEvent(event_uuid=event_id)
        row.match_uuid = match_uuid
        row.team_code = team_code
        row.device_id = device_id
        _apply_live_fields(row, event)
        row.provisional = True
        try:
            row.save()
        except NotUniqueError:
            # Concurrent insert of the same id won the race — reload and update
            # (latest wins) unless it has since been reconciled.
            row = MatchEvent.objects(event_uuid=event_id).first()
            if row is not None and row.provisional:
                _apply_live_fields(row, event)
                row.save()


def _match_window(recorded_at, duration_s, uploaded_dates):
    """Time span of the uploaded match, widened by a clock-skew margin, or None.

    Prefers [recorded_at, recorded_at + duration_s]; folds in the span of the
    uploaded events' own dates so late/early events are still covered.
    """
    start = recorded_at
    end = None
    if recorded_at is not None and duration_s is not None:
        try:
            end = recorded_at + timedelta(seconds=float(duration_s))
        except (TypeError, ValueError):
            end = None
    if uploaded_dates:
        earliest, latest = min(uploaded_dates), max(uploaded_dates)
        start = min(start, earliest) if start is not None else earliest
        end = max(end, latest) if end is not None else latest
    if start is None and end is None:
        return None
    start = start if start is not None else end
    end = end if end is not None else start
    return (start - RECONCILE_WINDOW_MARGIN, end + RECONCILE_WINDOW_MARGIN)


def reconcile_session_events(match_uuid, team_code, device_id, events,
                             recorded_at=None, duration_s=None):
    """Reconcile an uploaded match's events into the timeline (authoritative).

    The uploaded ``events[]`` are the source of truth: each is upserted
    (provisional=False), keyed by event id, preserving any ``coach_label``.
    Because the watch has no wire delete, an event the player UNDID simply never
    arrives in the upload — so provisional live-log events from the same device
    within this match's time window that are absent from the upload are dropped.

    Scope is device + time, NEVER the live ``match_uuid`` (an unreliable
    per-launch id, shared across matches in one app launch). Idempotent on
    re-upload: reconciled rows (provisional=False) are never dropped.
    """
    uploaded_ids = set()
    uploaded_dates = []
    for event in events or []:
        event_id = _event_id(event)
        if not event_id:
            continue
        uploaded_ids.add(event_id)
        date = _parse_timestamp(event.get('date'))
        if date is not None:
            uploaded_dates.append(date)
        row = MatchEvent.objects(event_uuid=event_id).first() or MatchEvent(event_uuid=event_id)
        row.match_uuid = match_uuid
        row.team_code = team_code
        row.device_id = device_id
        row.kind = event.get('kind')
        row.date = date
        row.note = event.get('note')
        row.source = event.get('source')
        row.provisional = False
        # coach_label is intentionally left untouched — preserved across reconcile.
        row.save()

    window = _match_window(recorded_at, duration_s, uploaded_dates)
    if window is None or not device_id:
        return
    start, end = window
    for row in MatchEvent.objects(device_id=device_id, provisional=True,
                                  date__gte=start, date__lte=end):
        if row.event_uuid not in uploaded_ids:
            row.delete()  # undone on the watch — never made it into the upload


# GET the unified team event timeline
@events_blueprint.route('/teams/<code>/events', methods=['GET'])
@auth.login_required
@rate_limited('read')
def get_team_events(code):
    """Every team member's tagged events within the last ``since_hours``
    (default 6), oldest-first. Names respect ``initials_only`` (§5); unknown
    ``kind`` strings pass through verbatim. Membership required; reads stay open
    to any member (no entitlement — a lapsed coach can still review)."""
    canonical = memberships.canonical_team_code(code) or code
    member_denial = memberships.require_member(canonical)
    if member_denial:
        return member_denial

    try:
        since_hours = int(request.args.get('since_hours', DEFAULT_SINCE_HOURS))
    except (TypeError, ValueError):
        since_hours = DEFAULT_SINCE_HOURS
    if since_hours <= 0:
        since_hours = DEFAULT_SINCE_HOURS

    memberships.ensure_team_roles(canonical)
    from match_tracks.models import DeviceTeamMembership
    member_id_by_device = {
        row.device_id: row.member_id
        for row in DeviceTeamMembership.objects(team_code=canonical, removed_at=None)}

    cutoff = datetime.utcnow() - timedelta(hours=since_hours)
    rows = MatchEvent.objects(team_code=canonical, date__gte=cutoff).order_by('date')

    events = []
    for row in rows:
        player = Player.objects(device_id=row.device_id).first() if row.device_id else None
        item = {
            'id': row.event_uuid,
            # Opaque per-team id (V3 §2); never the device id.
            'member_id': member_id_by_device.get(row.device_id),
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
    # Resolve by event id alone. The path match_uuid may be the client's
    # unreliable per-launch live id (never the session uuid), so a coach_label
    # written against it must still land — and survive reconciliation, which
    # also keys by event id. Event ids are globally unique uuids.
    row = MatchEvent.objects(event_uuid=str(event_id).strip().lower()).first()
    if row is None:
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
