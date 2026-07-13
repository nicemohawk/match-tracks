from datetime import datetime

from flask_mongoengine import MongoEngine
from marshmallow_mongoengine import ModelSchema

db = MongoEngine()


# Models
class Session(db.EmbeddedDocument):
    track = db.LineStringField()
    recorded_at = db.DateTimeField(required=True, default=datetime.now)


class Field(db.EmbeddedDocument):
    track = db.LineStringField()
    recorded_at = db.DateTimeField(required=True, default=datetime.now)


class Device(db.Document):
    name = db.StringField(required=False)
    vendor_identifier = db.StringField(max_length=36, unique=True, required=True)
    sessions = db.EmbeddedDocumentListField(Session)
    fields = db.EmbeddedDocumentListField(Field)


# Schemas
class SessionSchema(ModelSchema):
    class Meta:
        model = Session


class FieldSchema(ModelSchema):
    class Meta:
        model = Field


class DeviceSchema(ModelSchema):
    class Meta:
        model = Device


# Community field database
#
# These top-level collections back the crowd-sourced field database. Devices
# upload trained field outlines and match GPS tracks; the field service (see
# match_tracks/field_service.py) fits oriented rectangles to those observations
# and merges observations of the same physical pitch into a single canonical
# CommunityField row. All uuid values are stored as lowercase strings.
class Team(db.Document):
    meta = {'collection': 'teams'}
    code = db.StringField(primary_key=True)
    name = db.StringField(null=True)
    created_at = db.DateTimeField(default=datetime.utcnow)


class Player(db.Document):
    meta = {'collection': 'players'}
    device_id = db.StringField(required=True, unique=True)
    name = db.StringField(null=True)
    team_code = db.StringField(null=True)
    updated_at = db.DateTimeField(default=datetime.utcnow)


class CommunityField(db.Document):
    meta = {
        'collection': 'community_fields',
        'indexes': [
            ('rect_center_lat', 'rect_center_lon'),
            'merged_into',
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
    created_at = db.DateTimeField(default=datetime.utcnow)
