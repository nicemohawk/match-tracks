class Config(object):
    DEBUG = False
    TESTING = False
    MONGODB_DB = 'matchdb'
    # DATABASE_URI = 'sqlite:///:memory:'

class ProductionConfig(Config):
    HOST = 'http://google.com'
    # DATABASE_URI = 'mysql://user@localhost/foo'

class DevelopmentConfig(Config):
    DEBUG = True

class TestingConfig(Config):
    TESTING = True