import os

from test_denorm_project.settings import *  # noqa

TOX_ENVIRONMENT = os.getenv("TOX_PARALLEL_ENV")
DB_SUFFIX = ""
if TOX_ENVIRONMENT:
    DB_SUFFIX = "_" + TOX_ENVIRONMENT

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql_psycopg2",
        "NAME": f"denorm_test{DB_SUFFIX}",
        "HOST": "localhost",
        "USER": "postgres",
        "PASSWORD": "",
    }
}
