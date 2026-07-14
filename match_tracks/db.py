"""Minimal MongoEngine integration replacing the unmaintained flask-mongoengine.

The app used exactly three things from flask-mongoengine: the `db` proxy
(db.Document / db.*Field), config-driven connection init, and
QuerySet.first_or_404(). This supplies those directly on top of mongoengine.
"""
import mongoengine
from flask import abort


class _MongoEngine:
    """`db` proxy: db.Document, db.EmbeddedDocument, db.StringField, db.DictField,
    db.ListField, db.EmbeddedDocumentListField, ... all resolve to the mongoengine
    attribute of the same name."""
    def __getattr__(self, name):
        return getattr(mongoengine, name)


db = _MongoEngine()


def init_app(app):
    """Register the default connection from config (MONGODB_DB / MONGODB_HOST,
    same keys flask-mongoengine read). Lazy — import/boot never fails if mongod
    is down."""
    settings = {'db': app.config['MONGODB_DB']}
    host = app.config.get('MONGODB_HOST')
    if host:
        settings['host'] = host
    mongoengine.connect(**settings)


def first_or_404(queryset):
    """First document, or abort(404) — flask-mongoengine parity."""
    document = queryset.first()
    if document is None:
        abort(404)
    return document
