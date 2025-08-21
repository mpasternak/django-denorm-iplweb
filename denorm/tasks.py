from celery import shared_task

from denorm import denorms


@shared_task(ignore_result=True)
def flush_single(pk: int):
    denorms.flush_single(pk)


@shared_task(ignore_result=True)
def flush_via_queue():
    from denorm.models import DirtyInstance

    for elem in DirtyInstance.objects.all():
        flush_single.apply_async(kwargs={"pk": elem.pk})
