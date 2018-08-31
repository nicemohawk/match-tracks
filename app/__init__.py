from flask import Flask
from flask_mongoengine import MongoEngine

app = Flask(__name__,  instance_relative_config=True)
app.config.from_object('config.DevelopmentConfig')
app.config.from_pyfile('config.py')

db = MongoEngine(app)

from . import routes
