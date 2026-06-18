import logging
import select
import sys
import time

import psycopg2
import psycopg2.extensions
from django.core.management.base import BaseCommand
from django.db import connection, connections

from denorm.db import const
from denorm.tasks import flush_via_queue

logger = logging.getLogger(__name__)

# psycopg2 errors that mean "the LISTEN connection is dead, reconnect."
_CONNECTION_ERRORS = (
    psycopg2.OperationalError,
    psycopg2.InterfaceError,
    # On hard backend termination the cursor's reply is empty and psycopg2
    # raises AttributeError on a NoneType inside its result parsing path.
    AttributeError,
)


class Command(BaseCommand):

    help = (
        "Runs a process that checks for dirty fields and updates them in regular intervals. "
        "Survives transient DB connection drops (failover, pg_terminate_backend, "
        "pooler reconnect) by re-establishing the LISTEN with exponential backoff."
    )

    # Base/cap for the reconnect backoff; reset to the base on every successful
    # (re)connection so a flapping DB never accumulates an ever-growing wait.
    base_backoff = 1.0
    max_backoff = 30.0

    # Finite select() timeout (seconds). A blocking (None) wait could never
    # notice a silently dead connection between NOTIFYs, nor wake to shut down;
    # a periodic keepalive poll surfaces a dead connection so handle() reconnects.
    select_timeout = 30.0

    # Live reconnect backoff, re-read by handle() after each loop so a reset
    # performed by _listen_loop on connect takes effect.
    _backoff = base_backoff

    def add_arguments(self, parser):
        parser.add_argument(
            "--run-once",
            action="store_true",
            help="Used for testing. Causes event loop to run once. ",
        )

    def handle(self, run_once=False, **options):
        self._backoff = self.base_backoff
        while True:
            try:
                self._listen_loop(run_once=run_once)
            except KeyboardInterrupt:
                sys.exit()
            except _CONNECTION_ERRORS as e:
                logger.warning(
                    "denorm_queue: lost LISTEN connection (%s: %s); "
                    "reconnecting in %.1fs",
                    type(e).__name__,
                    e,
                    self._backoff,
                )
                # Drop Django's cached connection so the next .cursor() call
                # establishes a fresh psycopg2 connection.
                try:
                    connections["default"].close()
                except Exception:
                    logger.exception("denorm_queue: error closing stale connection")
                time.sleep(self._backoff)
                self._backoff = min(self.max_backoff, self._backoff * 2)
                if run_once:
                    return
                continue
            else:
                # Clean exit (run_once=True path) — don't loop forever.
                return

    def _listen_loop(self, run_once=False):
        """Inner loop. Raises on connection loss; caller reconnects."""
        # The cursor is only needed to issue LISTEN; close it promptly (a
        # long-lived daemon must not leak one cursor per reconnect). LISTEN is
        # registered on the connection itself and outlives the cursor.
        with connection.cursor() as crs:
            pg_con = connection.connection
            pg_con.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
            crs.execute(f"LISTEN {const.DENORM_QUEUE_NAME}")

        # We connected: clear any accumulated reconnect backoff so the next drop
        # starts waiting from the base again.
        self._backoff = self.base_backoff

        logger.info("denorm_queue: listening on channel '%s'", const.DENORM_QUEUE_NAME)

        # Spec 1.2: PostgreSQL does not queue NOTIFYs for disconnected
        # listeners. Dirty rows accumulated while we were down (deploy,
        # failover) would otherwise sit until the next unrelated write —
        # kick one flush for the backlog. Singleton dedups if one is queued.
        flush_via_queue.delay()

        ran_once = False
        while True:
            if ran_once and run_once:
                return
            ran_once = True

            try:
                ready = select.select([pg_con], [], [], self.select_timeout)
            except InterruptedError:
                # A signal (e.g. SIGTERM during graceful shutdown) interrupted
                # the wait. Loop again; KeyboardInterrupt still propagates to
                # handle() for a clean exit.
                continue
            # poll() raises on a dead connection — propagate so handle() can
            # reconnect with backoff. On a keepalive timeout we still poll, so a
            # connection that died silently between NOTIFYs is surfaced.
            pg_con.poll()
            # Spec 1.1: poll() appends every NOTIFY to pg_con.notifies and
            # never removes them — drain, or this daemon leaks memory under
            # sustained write traffic. The payload is empty; arrival is the
            # only signal.
            had_notifications = bool(pg_con.notifies)
            del pg_con.notifies[:]
            if ready == ([], [], []) and not had_notifications:
                # Pure keepalive wakeup, no work to do.
                continue
            flush_via_queue.delay()
