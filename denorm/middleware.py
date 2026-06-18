import logging

from django.conf import settings
from django.utils import deprecation

from denorm import flush
from denorm.models import DirtyInstance

logger = logging.getLogger(__name__)


class DenormMiddleware(deprecation.MiddlewareMixin):
    """
    Flushes denormalized fields during the response stage of a request, so data
    written during the request is recomputed before the next one. If your data
    mostly or only changes during requests this is a good default. As usual the
    order of middleware classes matters — put ``DenormMiddleware`` after any
    transaction middleware so committed markers are visible when it runs.

    Behaviour is controlled by ``DENORM_MIDDLEWARE_FLUSH`` (default ``"inline"``):

    * ``"inline"`` — call ``denorm.flush()`` synchronously in the response cycle.
    * ``"queue"``  — dispatch ``denorm.tasks.flush_via_queue`` to Celery and
      return immediately, so the request is not blocked by the flush. Use this
      when you already run Celery workers for denorm.
    * ``"off"``    — never flush here; rely on the ``denorm_queue`` LISTEN/NOTIFY
      daemon (or a manual/cron ``denorm_flush``) instead.

    In ``inline`` and ``queue`` modes the flush is skipped entirely unless there
    is at least one ``DirtyInstance`` marker. ``exists()`` is a cheap LIMIT-1
    query and is correct even for bulk/raw writes, because markers are created
    by database triggers rather than by Python signals.

    Note: in ``inline`` mode ``flush()`` retries transient Postgres errors
    (40001 / 40P01) internally via ``denorm.retry``. Anything that still
    surfaces here is either a non-retryable database error or a retry-exhausted
    deadlock, and should NOT be silently swallowed — the request would otherwise
    return 200 OK while denorm state is left inconsistent.
    """

    def process_response(self, request, response):
        # Read the setting LIVE so override_settings(DENORM_MIDDLEWARE_FLUSH=...)
        # works in tests and runtime reconfiguration takes effect.
        mode = getattr(settings, "DENORM_MIDDLEWARE_FLUSH", "inline")

        if mode == "off":
            return response

        if not DirtyInstance.objects.exists():
            return response

        if mode == "queue":
            from denorm.tasks import flush_via_queue

            flush_via_queue.delay()
        else:
            flush()

        return response
