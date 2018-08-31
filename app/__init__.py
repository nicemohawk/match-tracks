from flask import Flask
from app import models
from flask_mongoengine import MongoEngine

app = Flask(__name__)
# app.config.from_pyfile('the-config.cfg')
db = MongoEngine(app)

from app import routes
