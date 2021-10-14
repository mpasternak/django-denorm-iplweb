import logging
import select
import sys

import psycopg2.extensions
from django.core.management.base import BaseCommand
from django.db import connection

from denorm import denorms
from denorm.db import const

logger = logging.getLogger(__name__)


class Command(BaseCommand):

    help = "Runs a process that checks for dirty fields and updates them in regular intervals."

    def add_arguments(self, parser):
        parser.add_argument(
            "--run-once",
            action="store_true",
            help="Used for testing. Causes event loop to run once. ",
        )
        parser.add_argument(
            "--disable-housekeeping",
            action="store_true",
            help="Disable housekeeping for this process",
        )

    def handle(self, run_once=False, disable_housekeeping=False, **options):
        crs = (
            connection.cursor()
        )  # get the cursor and establish the connection.connection
        pg_con = connection.connection
        pg_con.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
        crs.execute(f"LISTEN {const.DENORM_QUEUE_NAME}")

        logger.info("Starting, running initial flush...")
        denorms.flush(disable_housekeeping=disable_housekeeping)

        logger.info(
            f"waiting for notifications on channel '{const.DENORM_QUEUE_NAME}'..."
        )
        while True:
            try:
                if select.select([pg_con], [], [], None) == ([], [], []):
                    logger.warning("timeout")
                else:
                    pg_con.poll()
                    while pg_con.notifies:
                        pg_con.notifies.pop()
                    denorms.flush(disable_housekeeping=disable_housekeeping)
            except KeyboardInterrupt:
                sys.exit()
            if run_once:
                break
