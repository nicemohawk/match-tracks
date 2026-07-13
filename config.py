import os


class Config(object):
    DEBUG = False
    TESTING = False
    MONGODB_DB = 'matchdb'
    # Accept any "Authorization: APIKey ..." value as an admin principal.
    # Off everywhere by default; turned on only in DevelopmentConfig below.
    # match_tracks.auth additionally hard-disables it whenever ENV=prod.
    DEV_ACCEPT_ANY_API_KEY = False


class ProductionConfig(Config):
    MONGODB_HOST = os.environ.get('MONGO_URL', None)


class DevelopmentConfig(Config):
    DEBUG = True
    # Local testing: let the watch/iOS app talk to a local server without
    # provisioning API keys. Set DEV_ACCEPT_ANY_API_KEY=0 in the environment
    # to opt out even in development.
    DEV_ACCEPT_ANY_API_KEY = os.environ.get('DEV_ACCEPT_ANY_API_KEY', '1') not in ('0', 'false', 'False')


class TestingConfig(Config):
    TESTING = True