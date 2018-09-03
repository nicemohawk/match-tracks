import os


class Config(object):
    DEBUG = False
    TESTING = False
    MONGODB_DB = 'matchdb'


class ProductionConfig(Config):
    MONGODB_HOST = os.environ.get('MONGO_URL', None)


class DevelopmentConfig(Config):
    DEBUG = True


class TestingConfig(Config):
    TESTING = True