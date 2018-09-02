from datetime import datetime

from flask_mongoengine import MongoEngine
from marshmallow_mongoengine import ModelSchema

db = MongoEngine()


class Session(db.EmbeddedDocument):
    data = db.LineStringField()
    recorded_at = db.DateTimeField(required=True, default=datetime.now)

    def __init__(self, data=None, recorded_at=None, *args, **kwargs):
        db.EmbeddedDocument.__init__(self, args, kwargs)

        if data is not None:
            self.data = data

        if recorded_at is not None:
            self.recorded_at = recorded_at


class Device(db.Document):
    name = db.StringField(required=False)
    vendor_identifier = db.StringField(max_length=36, unique=True, required=True)
    sessions = db.EmbeddedDocumentListField(Session)


# Schemas
class SessionSchema(ModelSchema):
    class Meta:
        model = Session


class DeviceSchema(ModelSchema):
    class Meta:
        model = Device
