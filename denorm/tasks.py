from celery import group, shared_task
from celery_singleton import Singleton

from denorm import denorms


@shared_task(base=Singleton, ignore_result=False)
def flush_single(content_type_id: int, object_id: int):
    # Task identity = the logical object being flushed, not an opaque
    # DirtyInstance pk. denorms.flush_single processes every marker for
    # the (content_type_id, object_id) pair, so a marker inserted after
    # the task was enqueued (or after some other path deleted the original
    # one) is still picked up.
    denorms.flush_single(content_type_id, object_id)
    return True


@shared_task(base=Singleton, ignore_result=False)
def flush_via_queue():
    from denorm.models import DirtyInstance

    pairs = list(
        DirtyInstance.objects.values_list("content_type_id", "object_id").distinct()
    )

    if pairs:
        job = group(
            flush_single.s(content_type_id=ct_id, object_id=obj_id)
            for ct_id, obj_id in pairs
        )
        return job.apply_async()
