import logging

import django

from denorm import flush

logger = logging.getLogger(__name__)


class DenormMiddleware:
    """
    Calls ``denorm.flush`` during the response stage of every request. If your data mostly or only changes during
    requests this should be a good idea. If you run into performance problems with this (because ``flush()`` takes
    to long to complete) you can try using a background process or handle flushing manually instead.

    As usual the order of middleware classes matters. It makes a lot of sense to put ``DenormMiddleware``
    after ``TransactionMiddleware`` in your ``MIDDLEWARE_CLASSES`` setting.

    Note: ``flush()`` retries transient Postgres errors (40001 / 40P01)
    internally via ``denorm.retry``. Anything that still surfaces here
    is either a non-retryable database error or a retry-exhausted
    deadlock, and should NOT be silently swallowed — the request would
    otherwise return 200 OK while denorm state is left inconsistent.
    """

    def process_response(self, request, response):
        flush()
        return response


if django.VERSION >= (1, 10):
    from django.utils import deprecation

    class DenormMiddleware(
        deprecation.MiddlewareMixin,
        DenormMiddleware,
    ):
        pass
