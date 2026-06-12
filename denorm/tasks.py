from celery import group, shared_task
from celery_singleton import Singleton

from denorm import denorms
from denorm.conf import settings


@shared_task(
    base=Singleton,
    ignore_result=False,
    lock_expiry=settings.DENORM_SINGLETON_LOCK_EXPIRY,
)
def flush_single(content_type_id: int, object_id: int):
    # Legacy single-pair task. Kept (one release minimum) so tasks already
    # sitting in brokers during a rolling deploy still execute. New code
    # dispatches flush_batch.
    # A flush legitimately outliving the expiry just allows a duplicate task —
    # safe: flush_single is concurrency-safe via skip_locked claims.
    denorms.flush_single(content_type_id, object_id)
    return True


@shared_task(
    base=Singleton,
    ignore_result=False,
    lock_expiry=settings.DENORM_SINGLETON_LOCK_EXPIRY,
)
def flush_batch(pairs):
    # One task per chunk of logical (content_type_id, object_id) pairs:
    # a 500k-row backlog must not become 500k broker messages. Overlap
    # with other workers degrades to skip_locked no-ops in flush_single.
    for content_type_id, object_id in pairs:
        denorms.flush_single(content_type_id, object_id)
    return True


@shared_task(
    base=Singleton,
    ignore_result=False,
    lock_expiry=settings.DENORM_SINGLETON_LOCK_EXPIRY,
)
def flush_via_queue():
    # A flush legitimately outliving the expiry just allows a duplicate task —
    # safe: flush_single is concurrency-safe via skip_locked claims.
    from denorm.models import DirtyInstance

    chunk_size = settings.DENORM_QUEUE_CHUNK_SIZE
    chunks = []
    chunk = []
    for pair in (
        DirtyInstance.objects.values_list("content_type_id", "object_id")
        .distinct()
        .iterator(chunk_size=2000)
    ):
        chunk.append(pair)
        if len(chunk) >= chunk_size:
            chunks.append(chunk)
            chunk = []
    if chunk:
        chunks.append(chunk)

    if chunks:
        job = group(flush_batch.s(pairs=c) for c in chunks)
        return job.apply_async()
