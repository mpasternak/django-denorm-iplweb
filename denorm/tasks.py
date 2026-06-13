import logging

from celery import chord, shared_task
from celery_singleton import Singleton

from denorm import denorms
from denorm.conf import settings

logger = logging.getLogger(__name__)


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


def _chunk_distinct_pairs():
    """Snapshot the distinct (content_type_id, object_id) dirty pairs and
    split them into chunks of DENORM_QUEUE_CHUNK_SIZE — one flush_batch task
    per chunk. Returns a list of chunks (each a list of pairs); empty when
    the dirty table is empty.
    """
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
    return chunks


def _remaining_content_type_ids():
    """Distinct content_type_ids still dirty — for the pass-cap error log."""
    from denorm.models import DirtyInstance

    return sorted(
        DirtyInstance.objects.values_list("content_type_id", flat=True).distinct()
    )


@shared_task(
    base=Singleton,
    ignore_result=False,
    lock_expiry=settings.DENORM_SINGLETON_LOCK_EXPIRY,
)
def flush_via_queue(_pass=0):
    # A flush legitimately outliving the expiry just allows a duplicate task —
    # safe: flush_single is concurrency-safe via skip_locked claims.
    #
    # Self-convergence: like inline denorms.flush(), the queue path must drain
    # cross-object cascade markers that are created WHILE a batch is being
    # processed (and whose marker INSERTs no longer NOTIFY, see migration
    # 0019). It does this with a chord: fan out the current snapshot into
    # flush_batch tasks, and when they all finish run _flush_requeue, which
    # re-dispatches flush_via_queue if the dirty table is still non-empty.
    # Bounded by DENORM_MAX_QUEUE_PASSES (the queue analogue of
    # DENORM_MAX_FLUSH_PASSES) so a non-convergent denorm cannot loop forever.
    chunks = _chunk_distinct_pairs()
    if not chunks:
        return

    if _pass >= settings.DENORM_MAX_QUEUE_PASSES:
        logger.error(
            "denorm flush_via_queue: aborting after %d passes; still-dirty "
            "content_type_ids=%s. A denormalized function is likely "
            "non-deterministic (returns a different value on every recompute), "
            "so flushing can never converge.",
            _pass,
            _remaining_content_type_ids(),
        )
        return

    return chord((flush_batch.s(pairs=c) for c in chunks))(
        _flush_requeue.s(next_pass=_pass + 1)
    )


@shared_task(ignore_result=False)
def _flush_requeue(batch_results, next_pass):
    # Chord callback: a batch round just finished. If markers remain (a
    # cross-object cascade created during the round), re-dispatch the next
    # pass. _pass varies the Singleton args so the re-dispatch is not deduped
    # against a stale lock from the previous pass.
    from denorm.models import DirtyInstance

    if DirtyInstance.objects.exists():
        flush_via_queue.delay(_pass=next_pass)
