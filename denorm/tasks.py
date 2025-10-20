from celery import group, shared_task
from celery_singleton import Singleton

from denorm import denorms


@shared_task(base=Singleton, ignore_result=False)
def flush_single(pk: int):
    from denorm.models import DirtyInstance

    try:
        res = DirtyInstance.objects.get(pk=pk)
    except DirtyInstance.DoesNotExist:
        return True
    denorms.flush_single(res.content_type_id, res.object_id, res.content_type)
    return True


@shared_task(base=Singleton, ignore_result=False)
def flush_via_queue():
    from denorm.models import DirtyInstance

    tasks = []
    for elem in DirtyInstance.objects.all():
        tasks.append(flush_single.s(pk=elem.pk))

    if tasks:
        job = group(tasks)
        return job.apply_async()
