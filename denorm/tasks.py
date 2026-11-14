import logging

from celery import chord, shared_task
from celery_singleton import Singleton

from django.db import InterfaceError, OperationalError, close_old_connections

from denorm import denorms
from denorm.conf import settings
from denorm.retry import is_connection_error

logger = logging.getLogger(__name__)

# Database errors that may mean "connection lost"; is_connection_error() then
# tells a lost connection apart from e.g. a query error.
_DB_ERRORS = (OperationalError, InterfaceError)


def _give_up_on_lost_connection(task_name, exc):
    """Log a lost database connection and drop the broken connection.

    A lost connection (restart, failover, crash) must not fail a denorm task:
    the dirty markers persist in the table, and the denorm_queue daemon
    re-dispatches flush_via_queue as soon as it reconnects (plus every
    keepalive), so the work resumes on its own. The daemon is the single place
    that escalates a long outage to an ERROR, after its grace period.
    """
    logger.warning(
        "denorm %s: database connection lost (%s: %s); giving up this run, "
        "the dirty markers persist and will be flushed once the database is back",
        task_name,
        type(exc).__name__,
        exc,
    )
    # Discard the broken connection so the next task in this worker process
    # opens a fresh one instead of failing on the dead socket.
    close_old_connections()


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
    #
    # Per-pair isolation: a single bad object MUST NOT stall the whole round.
    # flush_single is wrapped in @retry_on_serialization_failure, so what
    # reaches here is either retry-exhausted or a non-transient error.
    # We log loudly (logger.exception preserves the full traceback and
    # exc_info) and CONTINUE to the next pair.  Do NOT re-raise: re-raising
    # fails the task, which prevents the chord callback (_flush_requeue) from
    # running, which is precisely the stall we are preventing.
    #
    # The failing object's DirtyInstance marker is NOT deleted — flush_single's
    # transaction rolled back, so the marker persists and will be retried on
    # the next convergence round.  If the object keeps failing,
    # DENORM_MAX_QUEUE_PASSES caps the number of re-dispatches and logs an
    # error, bounding recovery latency without blocking other objects.
    #
    # A LOST CONNECTION is the exception to per-pair isolation: every remaining
    # pair would fail the same way and log a traceback each. Abort the batch
    # instead (markers persist; see _give_up_on_lost_connection).
    errors = 0
    for done, (content_type_id, object_id) in enumerate(pairs):
        try:
            denorms.flush_single(content_type_id, object_id)
        except _DB_ERRORS as exc:
            if is_connection_error(exc):
                _give_up_on_lost_connection("flush_batch", exc)
                return {"processed": done, "errors": errors, "aborted": True}
            errors += 1
            logger.exception(
                "denorm flush_batch: database error flushing "
                "(content_type_id=%s, object_id=%s); skipping pair so the "
                "rest of the batch and the chord callback can still run. "
                "The object's DirtyInstance marker persists for the next round.",
                content_type_id,
                object_id,
            )
        except Exception:
            errors += 1
            logger.exception(
                "denorm flush_batch: unhandled error flushing "
                "(content_type_id=%s, object_id=%s); skipping pair so the "
                "rest of the batch and the chord callback can still run. "
                "The object's DirtyInstance marker persists for the next round.",
                content_type_id,
                object_id,
            )
    return {"processed": len(pairs), "errors": errors}


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
    try:
        chunks = _chunk_distinct_pairs()
    except _DB_ERRORS as exc:
        if not is_connection_error(exc):
            raise
        _give_up_on_lost_connection("flush_via_queue", exc)
        return
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

    try:
        still_dirty = DirtyInstance.objects.exists()
    except _DB_ERRORS as exc:
        if not is_connection_error(exc):
            raise
        _give_up_on_lost_connection("_flush_requeue", exc)
        return
    if still_dirty:
        flush_via_queue.delay(_pass=next_pass)
