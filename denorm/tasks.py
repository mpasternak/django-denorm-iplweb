from celery import shared_task
from celery_singleton import Singleton

from denorm import denorms


@shared_task(base=Singleton, ignore_result=True)
def flush_single(pk: int):
    denorms.flush_single(pk)


@shared_task(base=Singleton, ignore_result=True)
def flush_via_queue():
    from denorm.models import DirtyInstance

    for elem in DirtyInstance.objects.all():
        flush_single.delay(pk=elem.pk)
