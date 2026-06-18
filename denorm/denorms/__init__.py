"""Denormalization core.

This package was split out of the former ``denorm/denorms.py`` module.

The flush engine (``flush``, ``flush_single``, ``_DirtyInstanceFlushProgress``
and the marker helpers) intentionally lives in this ``__init__`` rather than a
submodule: tests and the queue path reach these names as
``denorm.denorms.<name>`` (``mock.patch("denorm.denorms.flush_single")``,
``mock.patch("denorm.denorms._DirtyInstanceFlushProgress")``), and ``flush``
calls those same names internally — keeping them here makes the patch target
and the internal reference the same object, preserving the original behavior.

The denorm descriptor classes and the trigger-set management helpers were
extracted into submodules and are re-exported below so that
``denorm.denorms.X`` keeps working unchanged.
"""

import logging
import random
import sys
import threading
from contextlib import nullcontext

from django.core.exceptions import FieldDoesNotExist
from django.db import close_old_connections, connection, transaction

from denorm.retry import retry_on_serialization_failure

# Re-export the denorm descriptor classes and trigger-set management helpers.
from .aggregate import (  # noqa: E402,F401
    AggregateDenorm,
    CountDenorm,
    SumDenorm,
    TriggerFilterQuery,
    TriggerWhereNode,
)
from .base import (  # noqa: E402,F401
    Denorm,
    get_alldenorms,
    many_to_many_post_save,
    many_to_many_pre_save,
)
from .callback import (  # noqa: E402,F401
    BaseCacheKeyDenorm,
    BaseCallbackDenorm,
    CacheKeyDenorm,
    CallbackDenorm,
)
from .triggerset import (  # noqa: E402,F401
    build_triggerset,
    drop_triggers,
    install_triggers,
    rebuild_instances_of,
    rebuildall,
)

logger = logging.getLogger(__name__)

# Thread-local guard set while flush_single is recomputing an object.
# @denorm_always_dirty consults flush_in_progress() so that flush's OWN
# recompute save() does NOT re-mark the object dirty (which would prevent
# flush from ever converging for an always-dirty model).
_flush_state = threading.local()


def flush_in_progress():
    """True while the current thread is inside flush_single's recompute save."""
    return getattr(_flush_state, "active", False)


def mark_dirty(*instances):
    """Explicitly mark whole objects dirty.

    Creates func_name=NULL markers — the only NULL markers the library
    produces besides rebuild_instances_of(). NULL means "recompute every
    denormalized field of this object" and takes precedence over
    field-level markers in flush_single.
    """
    if any(instance.pk is None for instance in instances):
        raise ValueError("mark_dirty() requires saved instances (pk is None).")

    from django.contrib.contenttypes.models import ContentType

    from denorm.models import DirtyInstance

    markers = [
        DirtyInstance(
            content_type=ContentType.objects.get_for_model(instance),
            object_id=instance.pk,
        )
        for instance in instances
    ]
    DirtyInstance.objects.bulk_create(markers, ignore_conflicts=True)


INTERACTIVE = False


class _DirtyInstanceFlushProgress:
    def __init__(self, stream=None, interval_range=(1.0, 3.0)):
        self.stream = stream or sys.stderr
        self.interval_range = interval_range
        self._bar = None
        self._thread = None
        self._stop = threading.Event()

    def __enter__(self):
        from tqdm import tqdm

        self._bar = tqdm(
            total=0,
            desc="denorm_dirtyinstance",
            unit="row",
            file=self.stream,
            leave=True,
            bar_format="{desc}: {total_fmt} left [{elapsed}]",
        )
        self._refresh()
        self._thread = threading.Thread(
            target=self._poll,
            name="denorm-flush-progress",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(self.interval_range) + 0.5)
        self._refresh()
        if self._bar is not None:
            self._bar.close()
        close_old_connections()
        return False

    def _poll(self):
        close_old_connections()
        try:
            while not self._stop.wait(random.uniform(*self.interval_range)):
                self._refresh()
        finally:
            close_old_connections()

    def _refresh(self):
        from denorm.models import DirtyInstance

        close_old_connections()
        try:
            remaining = DirtyInstance.objects.count()
        except Exception:
            logger.exception("denorm flush progress monitor failed")
            self._stop.set()
            return

        self._set_remaining(remaining)

    def _set_remaining(self, remaining):
        if self._bar is None:
            return

        self._bar.n = 0
        self._bar.total = remaining
        self._bar.refresh()


def _markers_for(content_type_id, object_id):
    """All markers for the logical pair, filtered so the 0017 expression
    index is fully usable.

    The unique index keys on COALESCE(object_id, -1); filtering the raw
    column would fall back to scanning the whole content-type prefix —
    O(backlog) per object, O(backlog^2) per flush.
    """
    from django.db.models import Value
    from django.db.models.functions import Coalesce

    from denorm.models import DirtyInstance

    return DirtyInstance.objects.alias(
        _oid=Coalesce("object_id", Value(-1))
    ).filter(
        content_type_id=content_type_id,
        _oid=-1 if object_id is None else object_id,
    )


def _claim_and_delete_markers(content_type_id, object_id):
    """Lock, snapshot and DELETE all claimable markers for the pair.

    Returns the set of claimed ``func_name`` values (an empty set when
    nothing is claimable; ``None`` membership means a whole-object marker).
    Deleting at claim time (inside the caller's transaction) frees the
    unique-index key, so a colliding marker INSERT — from this transaction's
    own save() triggers or from a concurrent writer — waits for our commit
    instead of being silently dropped by the unique_violation handler.
    See docs/spec-concurrency-performance-fixes.md item 1.5.
    """
    from denorm.models import DirtyInstance

    locked_pks = list(
        _markers_for(content_type_id, object_id)
        .select_for_update(skip_locked=True)
        .values_list("pk", flat=True)
    )
    if not locked_pks:
        return set()
    func_names = set(
        DirtyInstance.objects.filter(pk__in=locked_pks).values_list(
            "func_name", flat=True
        )
    )
    DirtyInstance.objects.filter(pk__in=locked_pks).delete()
    return func_names


def _build_save_kwargs(
    obj, func_names, disable_autotime_during_flush, autotime_field_names
):
    """Translate claimed marker func_names into save() keyword arguments.

    NULL (None) in ``func_names`` -> full save (no update_fields); otherwise
    ``update_fields`` of the validated denorm field names, minus any auto_now
    field exclusions when autotime suppression is enabled during flush.
    """
    kw = {}
    if None not in func_names:
        update_fields = []
        for func_name in func_names:
            try:
                obj._meta.get_field(func_name)
                update_fields.append(func_name)
            except FieldDoesNotExist:
                continue
        if update_fields:
            kw["update_fields"] = update_fields

    if disable_autotime_during_flush and autotime_field_names:
        # Build an explicit update_fields that EXCLUDES auto_now fields,
        # so save() doesn't touch them. This replaces the old
        # suppress_autotime() approach which mutated class-level
        # Field.auto_now and leaked across threads.
        if "update_fields" in kw:
            kw["update_fields"] = [
                f for f in kw["update_fields"] if f not in autotime_field_names
            ]
        else:
            kw["update_fields"] = [
                f.name
                for f in obj._meta.local_fields
                if not f.primary_key and f.name not in autotime_field_names
            ]

    return kw


@retry_on_serialization_failure
def flush_single(content_type_id, object_id, content_type=None):
    from denorm.conf import settings

    disable_autotime_during_flush = settings.DENORM_DISABLE_AUTOTIME_DURING_FLUSH
    autotime_field_names = settings.DENORM_AUTOTIME_FIELD_NAMES

    if content_type is None:
        from django.contrib.contenttypes.models import ContentType

        content_type = ContentType.objects.get_for_id(content_type_id)

    with transaction.atomic():
        klass = content_type.model_class()

        # Lock the object row before claiming markers; every acquisition
        # uses skip_locked, so flush workers never wait on each other and
        # this ordering cannot deadlock flush-vs-flush.
        try:
            obj = klass.objects.select_for_update(of=("self",), skip_locked=True).get(
                pk=object_id
            )
        except klass.DoesNotExist:
            # Either the row is locked by another worker, or it has been
            # deleted between marker creation and this lookup. Distinguish:
            # if the row truly doesn't exist, we own the cleanup; otherwise
            # leave the markers untouched for whoever holds the lock.
            if klass.objects.filter(pk=object_id).exists():
                return  # locked elsewhere; another worker will handle it
            _claim_and_delete_markers(content_type.pk, object_id)
            return

        # Suppress flush-internal NOTIFY: the statement-level trigger on
        # denorm_dirtyinstance (migration 0019) skips pg_notify when
        # denorm.flushing = 'on'. SET LOCAL is transaction-scoped (auto-reset
        # at commit/rollback) and applies to THIS connection, so the marker
        # INSERTs fired by obj.save() below — same transaction, same
        # connection — see it and do NOT wake the queue for work this flush is
        # already doing. Genuine writes on other connections are unaffected.
        # Set here (after the object lock, before any save) so every path that
        # calls obj.save() has it; the early-return paths above never save.
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL denorm.flushing = 'on'")

        # Claim AND DELETE the markers now, before save(): while a claimed
        # marker still exists, the unique index silently swallows identical
        # marker inserts (our own save's triggers, concurrent writers),
        # losing invalidations.
        func_names = _claim_and_delete_markers(content_type.pk, object_id)
        if not func_names:
            return

        # Convergence loop (audit spec 2.5): a save() that changes a stored
        # value fires triggers that insert NEW markers for this same object
        # (a same-model denorm chain, e.g. full_name -> letterhead). Those
        # markers are created in OUR transaction and are visible to a re-claim
        # before commit (the same MVCC fact spec 1.5 relies on), so we process
        # them here instead of leaving them for another outer flush() pass.
        #
        # Reusing the locked, in-memory `obj` across iterations is safe: we
        # hold select_for_update(of=('self',)) on the row for the whole
        # transaction, so no concurrent write can change this object's own
        # source columns, and the own-column trigger exclusion means our own
        # saves never re-mark themselves. The re-claimed markers are therefore
        # genuine downstream chain links (recomputed from in-memory denorm
        # fields prior pre_save already refreshed) or dependency-driven markers
        # (recomputed via a fresh query) — never stale in-memory source data.
        #
        # Scope discipline: the re-claim filters to THIS (ct, oid) only;
        # cascade markers for OTHER objects are left for the normal flush path.
        #
        # Guard window: while we call obj.save() to recompute, set the
        # flush-in-progress flag so @denorm_always_dirty's post_save handler
        # does NOT re-mark this object (otherwise flush would never converge
        # for an always-dirty model). Save/restore the previous value so the
        # retry decorator's re-invocation, nested calls, and the early-return
        # paths above (which never reach here) all behave correctly.
        prev_flush_active = getattr(_flush_state, "active", False)
        _flush_state.active = True
        try:
            for passes_left in range(settings.DENORM_MAX_CONVERGE_PASSES, 0, -1):
                obj.save(
                    **_build_save_kwargs(
                        obj,
                        func_names,
                        disable_autotime_during_flush,
                        autotime_field_names,
                    )
                )
                if passes_left == 1:
                    # Cap reached: do NOT re-claim. Any markers our last save
                    # inserted (non-deterministic denorm functions, or a chain
                    # deeper than the cap) stay in the table and are handled by
                    # flush()'s outer loop, bounded by DENORM_MAX_FLUSH_PASSES.
                    break
                func_names = _claim_and_delete_markers(content_type.pk, object_id)
                if not func_names:
                    break
        finally:
            _flush_state.active = prev_flush_active


def flush(
    run_once=False,
    *,
    progress=False,
    progress_stream=None,
    progress_interval=(1.0, 3.0),
):
    """
    Updates all model instances marked as dirty by the DirtyInstance
    model.
    After this method finishes the DirtyInstance table is empty and
    all denormalized fields have consistent data.

    If progress is true, a tqdm-style counter periodically queries the
    DirtyInstance table and displays the current number of rows left.
    """

    # Loop until break.
    # We may need multiple passes, because an update on one instance
    # may cause an other instance to be marked dirty (dependency chains)

    # Get all dirty markers

    ran_once = False

    from denorm.conf import settings

    from denorm.models import DirtyInstance

    progress_context = (
        _DirtyInstanceFlushProgress(
            stream=progress_stream,
            interval_range=progress_interval,
        )
        if progress
        else nullcontext()
    )

    with progress_context:
        passes = 0
        while True:
            if run_once and ran_once:
                break

            if passes >= settings.DENORM_MAX_FLUSH_PASSES:
                remaining = list(
                    DirtyInstance.objects.values_list(
                        "content_type_id", flat=True
                    ).distinct()
                )
                if not remaining:
                    # Converged exactly on the final allowed pass.
                    return
                from django.contrib.contenttypes.models import ContentType

                remaining_labels = sorted(
                    f"{ct.app_label}.{ct.model}"
                    for ct in ContentType.objects.filter(pk__in=remaining)
                )
                logger.error(
                    "denorm.flush: aborting after %d passes; still-dirty "
                    "models=%s. A denormalized function is likely "
                    "non-deterministic (returns a different value on every "
                    "recompute), so flushing can never converge.",
                    passes,
                    remaining_labels,
                )
                return
            passes += 1

            # NOTE: it is tempting to rewrite this DISTINCT over the
            # COALESCE(object_id, -1) expression so it "matches" the migration
            # 0017 index. Don't — it does not help. EXPLAIN (ANALYZE, BUFFERS)
            # on a 60k-marker table (measured on both PostgreSQL 16.13 and
            # 18.4) shows the planner already picks the cheapest plan: a
            # HashAggregate over a single Seq Scan of this narrow 2-column
            # projection (~442 buffers, identical on both versions). The
            # alternatives are all worse:
            #   * COALESCE rewrite + ORDER BY: same HashAggregate+SeqScan with
            #     an extra Sort bolted on top — strictly more expensive.
            #   * Forcing the expression index: a full Index Scan + Unique
            #     (~60k buffers, "Index Searches: 1" — no skipping).
            #   * Recursive-CTE loose scan (~18k buffers): only wins when the
            #     distinct cardinality is tiny vs the row count, which it never
            #     is here — the 0017 unique index dedupes by func_name, so
            #     distinct (content_type_id, object_id) cardinality stays high.
            # PostgreSQL 18 native B-tree skip scan does NOT engage for this
            # DISTINCT-over-COALESCE shape (verified on 18.4: still a full Index
            # Scan when forced), so it changes nothing here either.
            # See test_flush_distinct for the NULL->None handling contract.
            processed = 0
            for content_type_id, object_id in (
                DirtyInstance.objects.all()
                .values_list("content_type_id", "object_id")
                .distinct()
                .iterator(chunk_size=2000)
            ):
                flush_single(content_type_id, object_id)
                processed += 1

            if not processed:
                return

            ran_once = True
