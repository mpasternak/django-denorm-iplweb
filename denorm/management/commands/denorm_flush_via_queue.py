import time

from django.core.management.base import BaseCommand, CommandError
from tqdm import tqdm

from denorm.models import DirtyInstance
from denorm.tasks import flush_via_queue


class Command(BaseCommand):
    help = (
        "Recalculates the value of every denormalized field that was marked "
        "dirty, dispatching the work to Celery (flush_via_queue) and waiting "
        "until the dirty table drains. Requires Celery workers consuming the "
        "denorm queue (or eager mode)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--timeout",
            type=float,
            default=300.0,
            help="Seconds to wait for the dirty table to drain (default 300).",
        )
        parser.add_argument(
            "--poll-interval",
            type=float,
            default=0.2,
            help="Seconds between dirty-table polls (default 0.2).",
        )

    def handle(self, timeout=300.0, poll_interval=0.2, **kwargs):
        total_rows = DirtyInstance.objects.count()
        if total_rows == 0:
            self.stdout.write(self.style.SUCCESS("No dirty instances to flush."))
            return

        self.stdout.write(f"Flushing {total_rows} dirty instance rows...")

        # Fire-and-forget. flush_via_queue fans the current snapshot into
        # flush_batch tasks via a chord whose callback (_flush_requeue)
        # re-dispatches the next pass until the dirty table is empty, bounded by
        # DENORM_MAX_QUEUE_PASSES. Convergence therefore spans MULTIPLE chords —
        # there is no single GroupResult to await — so we dispatch once and poll
        # the dirty table for completion instead.
        flush_via_queue.delay()

        deadline = time.monotonic() + timeout
        with tqdm(total=total_rows, desc="Flushing", unit="row") as pbar:
            while True:
                remaining = DirtyInstance.objects.count()
                # Cascades can transiently add markers, so remaining may exceed
                # the initial total; clamp progress to [0, total_rows].
                pbar.n = max(0, total_rows - remaining)
                pbar.refresh()
                if remaining == 0:
                    break
                if time.monotonic() >= deadline:
                    raise CommandError(
                        f"Timed out after {timeout}s with {remaining} dirty "
                        "rows still pending. Are Celery workers running and "
                        "consuming the denorm queue?"
                    )
                time.sleep(poll_interval)

        self.stdout.write(
            self.style.SUCCESS(f"Successfully flushed {total_rows} dirty rows.")
        )
