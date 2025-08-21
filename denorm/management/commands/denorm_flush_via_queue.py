from django.core.management.base import BaseCommand

from denorm.tasks import flush_via_queue


class Command(BaseCommand):
    help = (
        "Recalculates the value of every denormalized field that was marked dirty using "
        "Celery queues."
    )

    def handle(self, **kwargs):
        flush_via_queue.apply_async()
