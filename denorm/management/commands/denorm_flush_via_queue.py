import time

from django.core.management.base import BaseCommand
from tqdm import tqdm

from denorm.models import DirtyInstance
from denorm.tasks import flush_via_queue


class Command(BaseCommand):
    help = (
        "Recalculates the value of every denormalized field that was marked dirty using "
        "Celery queues."
    )

    def handle(self, **kwargs):
        # Get the total count before starting
        total_count = DirtyInstance.objects.count()

        if total_count == 0:
            self.stdout.write(self.style.SUCCESS("No dirty instances to flush."))
            return

        self.stdout.write(f"Flushing {total_count} dirty instances...")

        # Start the flush task
        result = flush_via_queue.apply_async()

        # Wait for the main task to spawn all subtasks
        time.sleep(0.5)

        # Get the group result
        group_result = result.get()

        if group_result is None:
            self.stdout.write(self.style.SUCCESS("No tasks to process."))
            return

        # Create progress bar
        with tqdm(total=total_count, desc="Flushing", unit="task") as pbar:
            while not group_result.ready():
                # Count completed tasks
                completed = group_result.completed_count()
                pbar.n = completed
                pbar.refresh()
                time.sleep(0.1)

            # Final update
            pbar.n = total_count
            pbar.refresh()

        self.stdout.write(
            self.style.SUCCESS(f"Successfully flushed {total_count} dirty instances.")
        )
