"""
This file attempts to automatically load the denorm backend for your chosen
database adaptor.

Currently only postgresql is supported. If your database is not detected then you can
specify the backend in your settings file:

DATABASES = {
    'default': {
        'DENORM_BACKEND': 'denorm.db.postgresql',
    }
}
"""

# Default mappings from common postgresql equivalents
from django.db import DEFAULT_DB_ALIAS, connections

DB_GUESS_MAPPING = {
    "postgresql_psycopg2": "postgresql",
}


def backend_for_dbname(db_name):
    return "denorm.db.%s" % DB_GUESS_MAPPING.get(db_name, db_name)


if "DENORM_BACKEND" in connections[DEFAULT_DB_ALIAS].settings_dict:
    backend = connections[DEFAULT_DB_ALIAS].settings_dict["DENORM_BACKEND"]
else:
    engine = connections[DEFAULT_DB_ALIAS].settings_dict["ENGINE"]
    backend = backend_for_dbname(engine.rsplit(".", 1)[1])
