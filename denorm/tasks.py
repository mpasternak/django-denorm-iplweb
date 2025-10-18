from celery import shared_task
from celery_singleton import Singleton

from denorm import denorms


@shared_task(base=Singleton, ignore_result=True)
def flush_single(pk: int):
    from denorm.models import DirtyInstance

    try:
        res = DirtyInstance.objects.get(pk=pk)
    except DirtyInstance.DoesNotExist:
        return True
    denorms.flush_single(res.content_type_id, res.object_id, res.content_type)


@shared_task(base=Singleton, ignore_result=True)
def flush_via_queue():
    from denorm.models import DirtyInstance

    for elem in DirtyInstance.objects.all():
        flush_single.delay(pk=elem.pk)
