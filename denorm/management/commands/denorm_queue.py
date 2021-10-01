import logging
import select
import sys

import psycopg2.extensions
from django.core.management.base import BaseCommand
from django.db import connection, transaction

from denorm import denorms
from denorm.db import const

logger = logging.getLogger(__name__)


def commit_manually(
    fn,
):  # replacement of transaction.commit_manually decorator removed in Django 1.6
    def _commit_manually(*args, **kwargs):
        transaction.set_autocommit(False)
        res = fn(*args, **kwargs)
        transaction.commit()
        transaction.set_autocommit(True)
        return res

    return _commit_manually


class Command(BaseCommand):

    help = "Runs a process that checks for dirty fields and updates them in regular intervals."

    def add_arguments(self, parser):
        parser.add_argument(
            "--run-once",
            action="store_true",
            help="Used for testing. Causes event loop to run once. ",
        )

    @commit_manually
    def handle(self, run_once=False, **options):
        crs = (
            connection.cursor()
        )  # get the cursor and establish the connection.connection
        pg_con = connection.connection
        pg_con.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
        crs.execute(f"LISTEN {const.DENORM_QUEUE_NAME}")

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
                        denorms.flush()
            except KeyboardInterrupt:
                transaction.commit()
                sys.exit()
            if run_once:
                break
