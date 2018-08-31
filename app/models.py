from mongoengine import *
from datetime import datetime


class Session(EmbeddedDocument):
    data = LineStringField()
    recorded_at = DateTimeField(required=True)

    def __init__(self, data, recorded_at=datetime.utcnow()):
        EmbeddedDocument.__init__(self)

        self.data = data
        self.recorded_at = recorded_at


class Device(Document):
    name = StringField(required=True)
    vendor_identifier = StringField(max_length=36, unique=True, required=True)
    sessions = EmbeddedDocumentListField(Session)



