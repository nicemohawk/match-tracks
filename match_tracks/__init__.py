import os

from flask import Flask
from flask_mongoengine import MongoEngine

app = Flask(__name__,  instance_relative_config=True)

app.url_map.strict_slashes = False

env = os.environ.get('ENV', None)

if env == "prod":
    app.config.from_object('config.ProductionConfig')
elif env == "test":
    app.config.from_object('config.TestingConfig')
else:  # dev
    app.config.from_object('config.DevelopmentConfig')


try:
    app.config.from_pyfile('config.py')
except FileNotFoundError as err:
    print("No instance config file found")


# app.config['MONGODB_SETTINGS'] = {
#         'host': os.environ['MONGO_URL'],
#         }

db = MongoEngine(app)

from . import routes
