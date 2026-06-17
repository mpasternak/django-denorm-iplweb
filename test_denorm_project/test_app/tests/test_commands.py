"""Management-command tests (denorm_sql / denorm_rebuild / ...)."""

from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.db import connection
from django.test import TransactionTestCase

import denorm
from denorm.management.commands.denorm_rebuild import Command as DenormRebuildCommand


class TtyStringIO(StringIO):
    def isatty(self):
        return True


class CommandsTestCase(TransactionTestCase):
    @patch("select.select")
    @patch("denorm.tasks.flush_via_queue")
    def test_denorm_queue(self, flush_via_queue, select):
        "denorm_queue kicks one flush for pre-existing backlog + one per NOTIFY wake-up."
        call_command("denorm_queue", run_once=True)
        select.assert_called_once()
        # One .delay() right after LISTEN (startup backlog, spec 1.2),
        # one after the select() wake-up.
        self.assertEqual(flush_via_queue.delay.call_count, 2)

    @patch("select.select")
    @patch("denorm.tasks.flush_via_queue")
    def test_denorm_queue_drains_notifications(self, flush_via_queue, select):
        "spec 1.1: poll()ed notifications must be drained, not accumulated forever."
        from django.db import connection

        connection.cursor()  # ensure the connection exists
        pg_con = connection.connection
        # Simulate notifications a previous poll() appended.
        pg_con.notifies.append(object())
        pg_con.notifies.append(object())

        call_command("denorm_queue", run_once=True)

        self.assertEqual(
            list(pg_con.notifies),
            [],
            "denorm_queue must drain pg_con.notifies after poll(); the list "
            "grows without bound in this long-running daemon otherwise.",
        )

    def test_makemigrations(self):
        "Test makemigrations command."
        call_command("makemigrations", verbosity=0)

    def test_denorm_init(self):
        "Test denorm_init command."
        call_command("denorm_init")

    def test_denorm_drop(self):
        "Test denorm_init command."
        call_command("denorm_drop")

    def test_denorm_flush(self):
        "Test denorm_init command."
        call_command("denorm_flush")

    def test_denorm_rebuild(self):
        "Test denorm_init command."
        call_command("denorm_rebuild")

    def test_denorm_rebuild_flush_uses_progress_on_tty(self):
        command = DenormRebuildCommand()
        progress_stream = TtyStringIO()
        command.stderr = progress_stream

        with (
            patch("denorm.denorms.rebuildall") as rebuildall,
            patch("denorm.denorms.flush") as flush,
        ):
            command.handle(no_flush=False, model_name="Forum", verbosity=1)

        rebuildall.assert_called_once_with(
            verbose=False,
            model_name="Forum",
            flush_=False,
        )
        flush.assert_called_once_with(
            run_once=False,
            progress=True,
            progress_stream=progress_stream,
        )

    def test_denorm_rebuild_flush_uses_no_progress_without_tty(self):
        command = DenormRebuildCommand()
        progress_stream = StringIO()
        command.stderr = progress_stream

        with (
            patch("denorm.denorms.rebuildall") as rebuildall,
            patch("denorm.denorms.flush") as flush,
        ):
            command.handle(no_flush=False, model_name=None, verbosity=2)

        rebuildall.assert_called_once_with(
            verbose=True,
            model_name=None,
            flush_=False,
        )
        flush.assert_called_once_with(
            run_once=True,
            progress=False,
            progress_stream=progress_stream,
        )

    def test_flush_uses_progress_context_when_requested(self):
        progress_stream = StringIO()

        with patch("denorm.denorms._DirtyInstanceFlushProgress") as progress:
            denorm.denorms.flush(
                progress=True,
                progress_stream=progress_stream,
                progress_interval=(1.0, 1.0),
            )

        progress.assert_called_once_with(
            stream=progress_stream,
            interval_range=(1.0, 1.0),
        )

    def test_denorm_sql(self):
        "Test denorm_init command."
        import sys

        sys.stdout = StringIO()

        call_command("denorm_sql")

        sys.stdout = sys.__stdout__
