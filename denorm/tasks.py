from celery import shared_task

from denorm import denorms


@shared_task
def flush_single(pk):
    denorms.flush_single(pk)
