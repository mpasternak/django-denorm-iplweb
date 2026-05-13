import os

from test_denorm_project.settings import *  # noqa

TOX_ENVIRONMENT = os.getenv("TOX_PARALLEL_ENV")
DB_SUFFIX = ""
if TOX_ENVIRONMENT:
    DB_SUFFIX = "_" + TOX_ENVIRONMENT

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("DJANGO_DB_NAME", f"denorm_test{DB_SUFFIX}"),
        "HOST": os.getenv("DJANGO_DB_HOST", os.getenv("DATABASE_HOST", "localhost")),
        "PORT": os.getenv("DJANGO_DB_PORT", os.getenv("DATABASE_PORT", "")),
        "USER": os.getenv("DJANGO_DB_USER", os.getenv("DATABASE_USER", "postgres")),
        "PASSWORD": os.getenv("DJANGO_DB_PASSWORD", os.getenv("DATABASE_PASSWORD", "")),
    }
}
