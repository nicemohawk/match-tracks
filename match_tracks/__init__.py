import os

from flask import Flask
from flask_mongoengine import MongoEngine

app = Flask(__name__,  instance_relative_config=True)

app.url_map.strict_slashes = False

env = os.environ.get('ENV', None)


if env == "prod":
    print("Loading production config")
    app.config.from_object('config.ProductionConfig')
elif env == "test":
    print("Loading testing config")
    app.config.from_object('config.TestingConfig')
else:  # dev
    print("Loading development config")
    app.config.from_object('config.DevelopmentConfig')


try:
    app.config.from_pyfile('config.py')
except FileNotFoundError as err:
    print("No instance config file found")

db = MongoEngine(app)

from . import routes

# V2 endpoint modules (see docs/backend-v2-architecture.md).
from match_tracks.live import live_blueprint
from match_tracks.comments import comments_blueprint
from match_tracks.formation import formation_blueprint
from match_tracks.privacy import privacy_blueprint
from match_tracks.memberships import memberships_blueprint
from match_tracks.team_admin import team_admin_blueprint
from match_tracks.entitlements import entitlements_blueprint
from match_tracks.seeding import seeding_blueprint

app.register_blueprint(live_blueprint)
app.register_blueprint(comments_blueprint)
app.register_blueprint(formation_blueprint)
app.register_blueprint(privacy_blueprint)
app.register_blueprint(memberships_blueprint)
app.register_blueprint(team_admin_blueprint)
app.register_blueprint(entitlements_blueprint)
app.register_blueprint(seeding_blueprint)
