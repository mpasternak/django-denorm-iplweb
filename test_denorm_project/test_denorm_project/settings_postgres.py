from test_denorm_project.settings import *  # noqa

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql_psycopg2",
        "NAME": "denorm_test",
        "HOST": "localhost",
        "USER": "postgres",
        "PASSWORD": "",
    }
}
