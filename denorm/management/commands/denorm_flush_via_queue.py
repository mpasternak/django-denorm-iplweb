import time

from django.core.management.base import BaseCommand, CommandError
from tqdm import tqdm

from denorm.models import DirtyInstance
from denorm.tasks import flush_via_queue


class Command(BaseCommand):
    help = (
        "Recalculates the value of every denormalized field that was marked "
        "dirty, using Celery queues. Requires a configured Celery result "
        "backend (the command waits on the dispatched task group)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--timeout",
            type=float,
            default=300.0,
            help="Seconds to wait for the dispatch task (default 300).",
        )

    def handle(self, timeout=300.0, **kwargs):
        total_rows = DirtyInstance.objects.count()
        if total_rows == 0:
            self.stdout.write(self.style.SUCCESS("No dirty instances to flush."))
            return

        self.stdout.write(f"Flushing {total_rows} dirty instance rows...")

        result = flush_via_queue.apply_async()
        try:
            group_result = result.get(timeout=timeout)
        except Exception as exc:  # no result backend, timeout, broker down
            raise CommandError(
                f"Could not obtain dispatch result ({exc!r}). This command "
                "requires a Celery result backend."
            )

        if group_result is None:
            self.stdout.write(self.style.SUCCESS("No tasks to process."))
            return

        total_tasks = len(group_result)
        with tqdm(total=total_tasks, desc="Flushing", unit="batch") as pbar:
            while not group_result.ready():
                pbar.n = group_result.completed_count()
                pbar.refresh()
                time.sleep(0.1)
            pbar.n = total_tasks
            pbar.refresh()

        self.stdout.write(
            self.style.SUCCESS(f"Successfully flushed {total_rows} dirty rows.")
        )
