import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "test_denorm_project.settings")

app = Celery("test_denorm_project")
# Pull CELERY_* keys from Django settings.
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
