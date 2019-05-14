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
