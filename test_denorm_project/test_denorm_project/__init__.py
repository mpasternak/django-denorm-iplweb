# Module is named celery_app.py (not celery.py) on purpose: runtests.py puts
# this package's directory on PYTHONPATH, where a celery.py would shadow the
# real `celery` package and cause a circular import.
from .celery_app import app as celery_app

__all__ = ("celery_app",)
