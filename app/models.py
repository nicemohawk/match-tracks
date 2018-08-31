from datetime import datetime

from mongoengine import *


class Session(EmbeddedDocument):
    data = LineStringField()
    recorded_at = DateTimeField(required=True, default=datetime.now)

    def __init__(self, data=None, recorded_at=None, *args, **kwargs):
        EmbeddedDocument.__init__(self, args, kwargs)

        if data != None:
            self.data = data

        if recorded_at != None:
            self.recorded_at = recorded_at


class Device(Document):
    name = StringField(required=True)
    vendor_identifier = StringField(max_length=36, unique=True, required=True)
    sessions = EmbeddedDocumentListField(Session)



