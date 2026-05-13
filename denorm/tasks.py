from celery import group, shared_task
from celery_singleton import Singleton
from django.db.models import Min

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

    # One subtask per distinct (content_type, object_id) — denorms.flush_single
    # processes ALL DirtyInstance rows for the pair internally, so spawning
    # one task per duplicate row just thrashes transactions and amplifies
    # lock contention on denorm_dirtyinstance.
    pks = list(
        DirtyInstance.objects.values("content_type_id", "object_id")
        .annotate(_pk=Min("pk"))
        .values_list("_pk", flat=True)
    )

    if pks:
        job = group(flush_single.s(pk=pk) for pk in pks)
        return job.apply_async()
