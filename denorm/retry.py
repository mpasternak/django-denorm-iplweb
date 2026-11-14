"""Retry transient PostgreSQL serialization failures and deadlocks.

A deadlock detected by Postgres (SQLSTATE 40P01) aborts the transaction.
Same for serialization failures (40001). The standard remedy is to retry
the whole transaction. denorm's flush path runs short transactions that
are safe to retry, so we wrap them.
"""

from __future__ import annotations

import functools
import logging
import random
import time

logger = logging.getLogger(__name__)

DEFAULT_MAX_RETRIES = 5
DEFAULT_BASE_DELAY = 0.05  # 50ms
DEFAULT_MAX_DELAY = 2.0

# Postgres SQLSTATEs we treat as transient.
RETRYABLE_PGCODES = frozenset({"40001", "40P01"})


def _pgcode(exc: BaseException) -> str | None:
    """Pull the PG SQLSTATE off a Django-wrapped exception, if present."""
    for candidate in (
        exc,
        getattr(exc, "__cause__", None),
        getattr(exc, "__context__", None),
    ):
        if candidate is None:
            continue
        pgcode = getattr(candidate, "pgcode", None)
        if pgcode:
            return pgcode
    return None


def is_retryable(exc: BaseException) -> bool:
    """True if `exc` is a transient Postgres error we should retry."""
    try:
        from django.db import OperationalError
    except ImportError:
        return False
    if not isinstance(exc, OperationalError):
        return False
    code = _pgcode(exc)
    if code in RETRYABLE_PGCODES:
        return True
    # Fallback: message-based match for cases where pgcode isn't propagated.
    msg = str(exc).lower()
    return "deadlock detected" in msg or "could not serialize access" in msg


# SQLSTATEs meaning "the server is going away / not accepting connections":
# admin_shutdown, crash_shutdown, cannot_connect_now (startup/recovery).
CONNECTION_LOSS_PGCODES = frozenset({"57P01", "57P02", "57P03"})

# Fallback substrings for errors raised client-side by psycopg2: a dropped
# socket has no SQLSTATE at all. Lowercased.
CONNECTION_LOSS_MESSAGES = (
    "server closed the connection unexpectedly",
    "connection already closed",
    "terminating connection",
    "the database system is shutting down",
    "the database system is starting up",
    "the database system is in recovery mode",
    "could not connect to server",
    "connection refused",
    "no connection to the server",
    "ssl connection has been closed unexpectedly",
    "could not receive data from server",
    "could not send data to server",
    "could not translate host name",
)


def is_connection_error(exc: BaseException) -> bool:
    """True if `exc` means the database connection is lost or unavailable.

    Covers a server restart, failover or crash, a dropped socket and a server
    refusing new connections while it shuts down or starts up. None of these
    is a problem with the denormalized data: the dirty markers stay in the
    table and are recomputed once the database is back, so callers log a
    warning and give up quietly instead of failing the task.
    """
    try:
        from django.db import InterfaceError, OperationalError
    except ImportError:
        return False
    import psycopg2

    if isinstance(exc, (InterfaceError, psycopg2.InterfaceError)):
        # psycopg2 raises InterfaceError ("connection already closed") when a
        # cursor is used on a connection whose socket is already gone.
        return True
    if not isinstance(exc, (OperationalError, psycopg2.OperationalError)):
        return False
    code = _pgcode(exc)
    if code is not None:
        # SQLSTATE class 08 = connection exception.
        return code.startswith("08") or code in CONNECTION_LOSS_PGCODES
    msg = str(exc).lower()
    return any(fragment in msg for fragment in CONNECTION_LOSS_MESSAGES)


def retry_on_serialization_failure(
    func=None,
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
):
    """Decorator: retry on Postgres 40001 / 40P01 with exponential backoff.

    Only safe for functions whose side effects are confined to the database
    transaction they manage. Do NOT use on code that performs external I/O
    (HTTP calls, sending mail, queuing celery tasks visible outside the tx).
    """

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            from django.db import connection

            # If we're already inside an outer transaction.atomic(), retry
            # is unsafe: Postgres aborts the whole top-level tx on
            # serialization_failure/deadlock_detected, so ROLLBACK TO
            # SAVEPOINT on the inner block fails, and any further SQL in
            # the outer block raises TransactionManagementError. Let the
            # error propagate so the OUTER caller can retry its own atomic.
            if connection.in_atomic_block:
                return fn(*args, **kwargs)

            attempt = 0
            while True:
                try:
                    return fn(*args, **kwargs)
                except Exception as exc:
                    if not is_retryable(exc) or attempt >= max_retries:
                        raise
                    attempt += 1
                    delay = min(
                        max_delay,
                        base_delay * (2 ** (attempt - 1)) * (0.5 + random.random()),
                    )
                    logger.warning(
                        "denorm.retry: %s on %s (attempt %d/%d), sleeping %.3fs",
                        type(exc).__name__,
                        fn.__qualname__,
                        attempt,
                        max_retries,
                        delay,
                    )
                    time.sleep(delay)

        return wrapper

    if func is not None:
        return decorator(func)
    return decorator
