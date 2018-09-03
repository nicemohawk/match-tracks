class Config(object):
    DEBUG = False
    TESTING = False
    MONGODB_DB = 'matchdb'


class ProductionConfig(Config):
    pass


class DevelopmentConfig(Config):
    DEBUG = True


class TestingConfig(Config):
    TESTING = True