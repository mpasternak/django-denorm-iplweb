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

    def add_arguments(self, parser):
        parser.add_argument(
            "--run-once",
            action="store_true",
            help="Used for testing. Causes event loop to run once. ",
        )

    def handle(self, run_once=False, **options):
        backoff = 1.0
        max_backoff = 30.0
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
                    backoff,
                )
                # Drop Django's cached connection so the next .cursor() call
                # establishes a fresh psycopg2 connection.
                try:
                    connections["default"].close()
                except Exception:
                    logger.exception("denorm_queue: error closing stale connection")
                time.sleep(backoff)
                backoff = min(max_backoff, backoff * 2)
                if run_once:
                    return
                continue
            else:
                # Clean exit (run_once=True path) — don't loop forever.
                return

    def _listen_loop(self, run_once=False):
        """Inner loop. Raises on connection loss; caller reconnects."""
        crs = connection.cursor()
        pg_con = connection.connection
        pg_con.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
        crs.execute(f"LISTEN {const.DENORM_QUEUE_NAME}")

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

            ready = select.select([pg_con], [], [], None)
            if ready == ([], [], []):
                logger.warning("denorm_queue: select() timeout")
                continue
            # Will raise on a dead connection — propagate so handle()
            # can reconnect with backoff.
            pg_con.poll()
            # Spec 1.1: poll() appends every NOTIFY to pg_con.notifies and
            # never removes them — drain, or this daemon leaks memory under
            # sustained write traffic. The payload is empty; arrival is the
            # only signal.
            del pg_con.notifies[:]
            flush_via_queue.delay()
