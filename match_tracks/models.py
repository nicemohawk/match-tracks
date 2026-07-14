from datetime import datetime

from marshmallow import Schema, fields, post_dump, post_load, EXCLUDE

from match_tracks.db import db


# Models
# track is a permissive DictField ({"coordinates": [[lat, lon], ...]}), not a
# GeoJSON LineStringField: an offline-first client legitimately produces empty
# or single-point tracks (GPS denied, indoor, very short match), and the strict
# LineString validator would 500 the whole batch on one trackless session. This
# matches the V2 Match.track store and avoids the GeoJSON lon/lat swap (the wire
# format is [lat, lon]). No code does geospatial queries against these embeds.
class Session(db.EmbeddedDocument):
    track = db.DictField()
    recorded_at = db.DateTimeField(required=True, default=datetime.now)
    uuid = db.StringField(null=True)  # V2: enables idempotent re-upload of the embedded copy


class Field(db.EmbeddedDocument):
    track = db.DictField()
    recorded_at = db.DateTimeField(required=True, default=datetime.now)
    uuid = db.StringField(null=True)  # V2: enables idempotent re-upload of the embedded copy


class Device(db.Document):
    name = db.StringField(required=False)
    vendor_identifier = db.StringField(max_length=36, unique=True, required=True)
    sessions = db.EmbeddedDocumentListField(Session)
    fields = db.EmbeddedDocumentListField(Field)


# Schemas
#
# Hand-rolled plain-marshmallow schemas replacing the marshmallow-mongoengine
# ModelSchema (that git fork pinned marshmallow and blocked Python 3.12+).
# These are byte-for-byte compatible with the old ModelSchema wire format:
#   - dump omits keys whose value is None (and empty-dict tracks), exactly as
#     the old ModelSchema did (it serialized the underlying mongoengine data);
#   - recorded_at is marshmallow's default iso DateTime (naive isoformat with
#     microseconds when present, no trailing 'Z') on dump, and tolerates a
#     trailing 'Z' on load (legacy clients send "2026-07-13T18:04:00Z");
#   - load() returns mongoengine model INSTANCES via @post_load, not dicts, and
#     tolerates unknown wire keys (Meta.unknown = EXCLUDE).
def _drop_empty(data):
    # Match the old ModelSchema, which serialized only the non-empty underlying
    # mongoengine data: a None value, an empty-dict track, or an empty
    # sessions/fields list was absent from the wire, not rendered as null/{}/[].
    return {key: value for key, value in data.items() if value not in (None, {}, [])}


class _EmbeddedSchema(Schema):
    """Shared behavior for the Session/Field embedded schemas."""
    class Meta:
        unknown = EXCLUDE

    uuid = fields.String(allow_none=True, load_default=None)
    track = fields.Raw(required=False, load_default=None)
    recorded_at = fields.DateTime(required=False)

    @post_dump
    def _drop_empty(self, data, **kwargs):
        return _drop_empty(data)


class SessionSchema(_EmbeddedSchema):
    @post_load
    def _make(self, data, **kwargs):
        return Session(**data)


class FieldSchema(_EmbeddedSchema):
    @post_load
    def _make(self, data, **kwargs):
        return Field(**data)


class DeviceSchema(Schema):
    class Meta:
        unknown = EXCLUDE

    id = fields.Function(
        lambda device: str(device.pk) if device.pk is not None else None,
        dump_only=True)
    name = fields.String(allow_none=True)
    vendor_identifier = fields.String(required=False)
    sessions = fields.Nested(SessionSchema, many=True, load_default=list)
    fields = fields.Nested(FieldSchema, many=True, load_default=list)

    @post_dump
    def _drop_empty(self, data, **kwargs):
        return _drop_empty(data)

    @post_load
    def _make(self, data, **kwargs):
        return Device(**data)


# Community field database
#
# These top-level collections back the crowd-sourced field database. Devices
# upload trained field outlines and match GPS tracks; the field service (see
# match_tracks/field_service.py) fits oriented rectangles to those observations
# and merges observations of the same physical pitch into a single canonical
# CommunityField row. All uuid values are stored as lowercase strings.
class Team(db.Document):
    meta = {'collection': 'teams', 'indexes': ['owner_device_id']}
    code = db.StringField(primary_key=True)
    name = db.StringField(null=True)
    requires_consent = db.BooleanField(default=False)  # V2 §5: minors teams gate rosters
    created_at = db.DateTimeField(default=datetime.utcnow)
    owner_device_id = db.StringField(null=True)  # lowercase device id; None = unowned
    archived = db.BooleanField(default=False)  # soft-archive (reversible)
    archived_at = db.DateTimeField(null=True)


class Player(db.Document):
    meta = {'collection': 'players'}
    device_id = db.StringField(required=True, unique=True)
    name = db.StringField(null=True)
    team_code = db.StringField(null=True)  # V2: now the DEFAULT team; memberships live in device_teams
    initials_only = db.BooleanField(default=False)  # V2 §5: render "B. L." everywhere
    consent_acknowledged_at = db.DateTimeField(null=True)  # V2 §5
    updated_at = db.DateTimeField(default=datetime.utcnow)


class CommunityField(db.Document):
    meta = {
        'collection': 'community_fields',
        'indexes': [
            ('rect_center_lat', 'rect_center_lon'),
            'merged_into',
            'sport_id',
        ],
    }
    uuid = db.StringField(primary_key=True)
    device_id = db.StringField(null=True)  # original uploader
    name = db.StringField(null=True)
    outline = db.ListField(null=True)  # raw [[lat, lon], ...]
    rect_center_lat = db.FloatField()
    rect_center_lon = db.FloatField()
    rect_length_m = db.FloatField()
    rect_width_m = db.FloatField()
    rect_heading_deg = db.FloatField()
    source = db.StringField()  # 'trained' | 'inferred' | 'community' (free strings)
    observation_count = db.IntField(default=1)
    confidence = db.FloatField()
    has_trained_observation = db.BooleanField(default=False)
    contributing_device_ids = db.ListField(db.StringField())
    sport_id = db.StringField(null=True)  # V2 §4: None means soccer
    seeded = db.BooleanField(default=False)  # V2 §8: from satellite imagery, unconfirmed
    created_at = db.DateTimeField(default=datetime.utcnow)
    merged_into = db.StringField(null=True)  # canonical uuid when this row is an alias stub


class Match(db.Document):
    meta = {
        'collection': 'matches',
        'indexes': ['device_id', 'team_code', '-recorded_at'],
    }
    uuid = db.StringField(primary_key=True)
    device_id = db.StringField(required=True)
    field_uuid = db.StringField(null=True)
    recorded_at = db.DateTimeField()
    duration_s = db.FloatField(null=True)
    track = db.DictField()  # {'coordinates': [...]}
    events = db.ListField(db.DictField())
    stats = db.DictField()
    team_code = db.StringField(null=True)
    sport_id = db.StringField(null=True)  # V2 §4: None means soccer
    created_at = db.DateTimeField(default=datetime.utcnow)


# V2 documents (see docs/backend-v2-architecture.md — the binding contract)

class LiveStatus(db.Document):
    """Latest live-match snapshot per device; overwritten on every accepted update."""
    meta = {'collection': 'live_status', 'indexes': ['team_code']}
    device_id = db.StringField(primary_key=True)
    team_code = db.StringField(null=True)
    match_uuid = db.StringField(null=True)
    sequence = db.IntField(default=0)
    updated_at = db.DateTimeField(default=datetime.utcnow)
    elapsed_s = db.FloatField(null=True)
    heart_rate = db.FloatField(null=True)
    distance_m = db.FloatField(null=True)
    current_speed = db.FloatField(null=True)
    on_pitch = db.BooleanField(default=True)
    us_goals = db.IntField(null=True)
    them_goals = db.IntField(null=True)
    x = db.FloatField(null=True)  # normalized field coords, when known
    y = db.FloatField(null=True)


class MatchComment(db.Document):
    """Team chat attached to a match. Soft-deleted rather than removed."""
    meta = {'collection': 'match_comments', 'indexes': ['match_uuid', 'posted_at']}
    uuid = db.StringField(primary_key=True)
    match_uuid = db.StringField(required=True)
    team_code = db.StringField(null=True)
    author_device = db.StringField(null=True)
    author_name = db.StringField(null=True)  # denormalized, initials-respecting at post time
    body = db.StringField(max_length=1000)
    posted_at = db.DateTimeField(default=datetime.utcnow)
    deleted = db.BooleanField(default=False)
    author_tombstoned = db.BooleanField(default=False)


class DeviceTeamMembership(db.Document):
    """A device may belong to many teams; players.team_code stays the default."""
    meta = {
        'collection': 'device_teams',
        'indexes': [{'fields': ['device_id', 'team_code'], 'unique': True}, 'team_code'],
    }
    device_id = db.StringField(required=True)
    team_code = db.StringField(required=True)
    joined_at = db.DateTimeField(default=datetime.utcnow)


class Entitlement(db.Document):
    """A verified StoreKit subscription for one device."""
    meta = {
        'collection': 'entitlements',
        'indexes': [{'fields': ['device_id', 'product_id'], 'unique': True}],
    }
    device_id = db.StringField(required=True)
    product_id = db.StringField(required=True)
    expires_at = db.DateTimeField(null=True)
    environment = db.StringField(null=True)


class SportProfile(db.Document):
    """Per-sport plausible pitch dimensions (config data, not code constants)."""
    meta = {'collection': 'sport_profiles'}
    sport_id = db.StringField(primary_key=True)
    min_length_m = db.FloatField(required=True)
    max_length_m = db.FloatField(required=True)
    min_width_m = db.FloatField(required=True)
    max_width_m = db.FloatField(required=True)


class SeedRequest(db.Document):
    """A queued satellite-imagery field-seeding job."""
    meta = {'collection': 'seed_requests', 'indexes': ['device_id', 'status']}
    uuid = db.StringField(primary_key=True)
    device_id = db.StringField(required=True)
    latitude = db.FloatField(required=True)
    longitude = db.FloatField(required=True)
    radius_m = db.FloatField(default=1500.0)
    requested_at = db.DateTimeField(default=datetime.utcnow)
    status = db.StringField(default='pending')  # pending | completed | failed
