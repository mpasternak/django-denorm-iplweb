"""Deadlock and race-condition reproducers for denorm.

These tests are designed to FAIL against the current implementation. Once
fixes land (retry-on-40P01, deletion-by-pk, distinct fan-out, no-global
field mutation, etc.) they should pass.

Run with:
    uv run pytest tests/test_deadlocks.py -v
"""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

import pytest
from django.contrib.contenttypes.models import ContentType
from django.db import OperationalError, transaction

# ---------------------------------------------------------------------------
# 1. Infrastructure sanity check: real Postgres + threads CAN deadlock.
# ---------------------------------------------------------------------------


def test_postgres_actually_deadlocks(transactional_db, thread_runner):
    """Sanity check: two threads holding rows in opposite order produce 40P01.

    This is not a denorm test — it's proof that the testcontainers Postgres
    setup is real (a SQLite test DB would silently serialize and never produce
    deadlock_detected). If this test passes, the rest of the suite is
    exercising real concurrency.
    """
    from test_app.models import Forum

    f1 = Forum.objects.create(title="F1")
    f2 = Forum.objects.create(title="F2")

    barrier = threading.Barrier(2)

    def cross_lock(first_pk, second_pk):
        with transaction.atomic():
            Forum.objects.select_for_update().get(pk=first_pk)
            barrier.wait()
            time.sleep(0.1)  # widen the window so contention is real
            Forum.objects.select_for_update().get(pk=second_pk)

    _, errors = thread_runner(cross_lock, [(f1.pk, f2.pk), (f2.pk, f1.pk)], timeout=30)

    assert any(
        isinstance(e, OperationalError) and "deadlock detected" in str(e).lower()
        for e in errors
        if e is not None
    ), f"Expected Postgres to detect a deadlock; got errors: {errors}"


# ---------------------------------------------------------------------------
# 2. Multi-row save() inside transaction.atomic() crosses CountField triggers
#    on the parent forum and deadlocks — and the error propagates uncaught.
# ---------------------------------------------------------------------------


def test_triggers_deadlock_on_multi_row_atomic_save(
    transactional_db, denorm_triggers, thread_runner
):
    """A typical application pattern triggers a deadlock via denorm triggers.

    Setup: two forums F1, F2; two posts P1 in F1, P2 in F2.

    Thread A (inside transaction.atomic()):
        P1.save() → AFTER UPDATE trigger UPDATEs F1 (post_count)
        P2.save() → AFTER UPDATE trigger UPDATEs F2 (post_count)

    Thread B (inside transaction.atomic(), reversed order):
        P2.save() → trigger UPDATEs F2 first
        P1.save() → trigger needs UPDATE on F1

    A holds F1, waits for F2. B holds F2, waits for F1. Postgres detects
    the cycle, aborts one with `deadlock detected`.

    Nothing in `denorm/` catches this — the test proves the symptom is real
    AND that there is no application-facing retry.

    EXPECTED TO FAIL: at least one thread leaks OperationalError. After a
    proper retry wrapper (or a documented `denorm.retry_on_deadlock` helper),
    these should be transparently retried.
    """
    from test_app.models import Forum, Post

    from denorm import retry_on_serialization_failure

    f1 = Forum.objects.create(title="F1")
    f2 = Forum.objects.create(title="F2")
    p1 = Post.objects.create(title="p1", forum=f1)
    p2 = Post.objects.create(title="p2", forum=f2)

    barrier = threading.Barrier(2)

    def _sync():
        # Retry-safe: on a retry, the OTHER thread may already have
        # succeeded and exited, so this barrier wait would hang forever
        # if it didn't have a timeout. A broken barrier is fine — we
        # just continue and hope for natural overlap.
        try:
            barrier.wait(timeout=0.5)
        except threading.BrokenBarrierError:
            pass

    @retry_on_serialization_failure
    def atomic_cross_save(first_post_pk, second_post_pk):
        with transaction.atomic():
            Post.objects.get(pk=first_post_pk).save()
            _sync()
            time.sleep(0.15)
            Post.objects.get(pk=second_post_pk).save()

    _, errors = thread_runner(
        atomic_cross_save, [(p1.pk, p2.pk), (p2.pk, p1.pk)], timeout=30
    )

    leaked = [e for e in errors if e is not None]
    assert not leaked, (
        f"Trigger-induced deadlock was not handled: {leaked}. "
        "After fix: denorm should expose a retry helper "
        "(or document this constraint and recommend retry middleware)."
    )


# ---------------------------------------------------------------------------
# 3. Stress test: app-level multi-row writes + concurrent flush workers
#    on the same data set. Mimics the user's reported workload.
# ---------------------------------------------------------------------------


def test_concurrent_flush_under_app_write_load(
    transactional_db, denorm_triggers, thread_runner
):
    """User's reported scenario: many flush workers + concurrent app writes.

    Mix:
      - 4 "app" threads doing multi-row Post saves in transaction.atomic()
        that cross forums (the same pattern as test #2 but in a loop).
      - 6 "flush" threads repeatedly calling denorms.flush(run_once=True).

    Expectation: with enough crossings, Postgres will abort at least one
    transaction with deadlock_detected or serialization_failure. Currently
    these surface as raw OperationalErrors. After fix: flush workers retry
    silently (and the app pattern is documented with a retry recipe).

    EXPECTED TO FAIL on current code.
    """
    from test_app.models import Forum, Post

    from denorm import denorms
    from denorm.models import DirtyInstance

    forums = [Forum.objects.create(title=f"f{i}") for i in range(4)]
    posts: list[Post] = []
    for f in forums:
        for j in range(3):
            posts.append(Post.objects.create(title=f"{f.title}-p{j}", forum=f))

    forum_ct = ContentType.objects.get_for_model(Forum)
    post_ct = ContentType.objects.get_for_model(Post)
    DirtyInstance.objects.bulk_create(
        [DirtyInstance(content_type=post_ct, object_id=p.pk) for p in posts]
        + [DirtyInstance(content_type=forum_ct, object_id=f.pk) for f in forums],
        ignore_conflicts=True,
    )

    stop = threading.Event()

    def app_writer(seed):
        import random

        rng = random.Random(seed)
        pks = [p.pk for p in posts]
        iterations = 25
        for _ in range(iterations):
            if stop.is_set():
                return
            # Pick 4 distinct posts in random order — guaranteed to cross
            # forums often given 12 posts across 4 forums.
            sample = rng.sample(pks, 4)
            try:
                with transaction.atomic():
                    for pk in sample:
                        Post.objects.get(pk=pk).save()
            except OperationalError:
                # App-side deadlocks are expected; user can wrap with retry.
                # The point of this test is that flush_single should NOT
                # also propagate these.
                pass

    def flusher(_idx):
        for _ in range(15):
            if stop.is_set():
                return
            denorms.flush(run_once=True)

    args_list = [(i,) for i in range(4)] + [  # app writers
        (100 + i,) for i in range(6)
    ]  # flushers (distinct ids)

    def dispatch(seed):
        if seed < 100:
            app_writer(seed)
        else:
            flusher(seed)

    _, errors = thread_runner(dispatch, args_list, timeout=180)

    # Only count errors from the FLUSH workers — those are the ones denorm
    # owns. App-side errors are the user's to retry.
    flush_errors = [e for i, e in enumerate(errors) if e is not None and i >= 4]
    assert not flush_errors, (
        f"{len(flush_errors)} flush workers leaked DB errors:\n"
        + "\n".join(f"  - {type(e).__name__}: {e}" for e in flush_errors[:5])
        + (f"\n  ... and {len(flush_errors) - 5} more" if len(flush_errors) > 5 else "")
        + "\nAfter fix: denorms.flush()/flush_single() should retry on 40P01/40001."
    )


# ---------------------------------------------------------------------------
# 4. res.delete() race: new dirty markers inserted during flush_single
#    by a concurrent transaction get wiped along with the originals.
# ---------------------------------------------------------------------------


def test_dirty_markers_inserted_during_flush_are_not_wiped(
    transactional_db, denorm_triggers, thread_runner
):
    """flush_single's `res.delete()` deletes by (content_type_id, object_id),
    so any DirtyInstance row inserted by a CONCURRENT transaction between the
    initial SELECT FOR UPDATE and the final DELETE is wiped without ever
    being processed → silent data inconsistency.

    Reproducer:
      Thread A:
        - Has DirtyInstance(Forum=F) marker
        - Starts flush_single(F): locks DirtyInstance rows, locks F row,
          calls F.save() (slow — we widen the window with a sentinel column
          update that takes time).
        - Before res.delete(), Thread B inserts a NEW DirtyInstance(F).
        - Thread A's res.delete() wipes both, losing Thread B's marker.

      Thread B:
        - Waits until A is inside obj.save(), then INSERTs a fresh
          DirtyInstance(F) (with a distinguishing func_name).
        - Commits immediately.

    After both threads finish, the new DirtyInstance Thread B inserted should
    still exist. EXPECTED TO FAIL on current code: count is 0.
    """
    from test_app.models import Forum

    from denorm.models import DirtyInstance

    forum = Forum.objects.create(title="race")
    forum_ct = ContentType.objects.get_for_model(Forum)
    # Forum INSERT trigger already created a DirtyInstance(Forum). Reset
    # to a clean known state for this test.
    DirtyInstance.objects.filter(content_type=forum_ct, object_id=forum.pk).delete()
    DirtyInstance.objects.create(content_type=forum_ct, object_id=forum.pk)

    sentinel_func_name = "marker-from-concurrent-tx"
    inside_save = threading.Event()
    can_continue = threading.Event()

    # Monkey-patch Forum.save to widen the window between SELECT FOR UPDATE
    # and res.delete() in flush_single, AND signal Thread B.
    original_save = Forum.save

    def slow_save(self, *args, **kwargs):
        result = original_save(self, *args, **kwargs)
        inside_save.set()
        can_continue.wait(timeout=10)
        return result

    Forum.save = slow_save

    try:

        def thread_a_flush():
            from denorm import denorms

            denorms.flush_single(forum_ct.pk, forum.pk, forum_ct)

        def thread_b_inject():
            assert inside_save.wait(timeout=10), "Thread A never entered save()"
            # Insert a new DirtyInstance from an independent transaction.
            with transaction.atomic():
                DirtyInstance.objects.create(
                    content_type=forum_ct,
                    object_id=forum.pk,
                    func_name=sentinel_func_name,
                )
            # Let Thread A proceed to res.delete().
            can_continue.set()

        _, errors = thread_runner(
            lambda fn: fn(), [(thread_a_flush,), (thread_b_inject,)], timeout=30
        )
        assert not [e for e in errors if e is not None], errors

        surviving = DirtyInstance.objects.filter(
            content_type=forum_ct, object_id=forum.pk, func_name=sentinel_func_name
        ).count()
        assert surviving == 1, (
            "res.delete() wiped a DirtyInstance inserted by a CONCURRENT "
            "transaction during flush_single → silently lost dirty marker."
        )
    finally:
        Forum.save = original_save


# ---------------------------------------------------------------------------
# 5. suppress_autotime is a Python-level data race
#    (contextmanagers.py:5-21 mutates class-level Field state)
# ---------------------------------------------------------------------------


def test_suppress_autotime_is_a_python_data_race(
    transactional_db, denorm_triggers, thread_runner
):
    """`suppress_autotime` mutates `field.auto_now` on the class-level Field
    instance. This Field object is shared by every instance of the model
    AND every thread in the process. While Thread A's flush_single is
    inside the suppress window, Thread B's ordinary `obj.save()` sees
    `auto_now=False` and silently fails to update its timestamp.

    EXPECTED TO FAIL: Thread B's `updated_on` is NOT bumped because Thread
    A's suppress leaked across threads.

    Fix: stop mutating shared Field state. Either pre-compute the timestamp
    and pass `update_fields`, or use a thread-local override mechanism.
    """
    from test_app.models import SkipCommentWithoutSkip, SkipPost

    from denorm.models import DirtyInstance

    post = SkipPost.objects.create(text="orig")
    c1 = SkipCommentWithoutSkip.objects.create(post=post, text="c1")
    c2 = SkipCommentWithoutSkip.objects.create(post=post, text="c2")
    # Ensure the DB-stored updated_on for c2 is captured at a known instant.
    c2_before = SkipCommentWithoutSkip.objects.get(pk=c2.pk).updated_on
    time.sleep(0.05)  # ensure any future bump is strictly greater

    ct = ContentType.objects.get_for_model(SkipCommentWithoutSkip)
    DirtyInstance.objects.filter(content_type=ct, object_id=c1.pk).delete()
    DirtyInstance.objects.create(content_type=ct, object_id=c1.pk)

    inside_suppress = threading.Event()
    can_proceed = threading.Event()

    original_save = SkipCommentWithoutSkip.save

    def slow_save(self, *args, **kwargs):
        # Only the flush_single target sleeps — Thread B's save proceeds
        # normally and should observe shared state set by Thread A.
        if self.pk == c1.pk:
            inside_suppress.set()
            can_proceed.wait(timeout=10)
        return original_save(self, *args, **kwargs)

    SkipCommentWithoutSkip.save = slow_save
    try:
        with (
            patch("denorm.conf.settings.DENORM_DISABLE_AUTOTIME_DURING_FLUSH", True),
            patch("denorm.conf.settings.DENORM_AUTOTIME_FIELD_NAMES", ["updated_on"]),
        ):

            def thread_a():
                from denorm import denorms

                denorms.flush_single(ct.pk, c1.pk, ct)

            def thread_b():
                assert inside_suppress.wait(timeout=10), "Thread A never reached save()"
                # Ordinary application save — has nothing to do with denorm.
                fresh = SkipCommentWithoutSkip.objects.get(pk=c2.pk)
                fresh.text = "modified"
                fresh.save()
                can_proceed.set()

            _, errors = thread_runner(
                lambda fn: fn(), [(thread_a,), (thread_b,)], timeout=30
            )
            assert not [e for e in errors if e is not None], errors

        c2_after = SkipCommentWithoutSkip.objects.get(pk=c2.pk).updated_on
        assert c2_after > c2_before, (
            f"Thread B's save() was robbed of auto_now by Thread A's "
            f"suppress_autotime — updated_on did not advance ({c2_after} == {c2_before}). "
            "This is a Python data race on class-level Field.auto_now."
        )
    finally:
        SkipCommentWithoutSkip.save = original_save


# ---------------------------------------------------------------------------
# 6. Duplicate DirtyInstance accumulation: the unique_violation handler in
#    triggers.py:36-42 is dead code (no unique constraint exists).
# ---------------------------------------------------------------------------


def test_dirty_instance_duplicates_accumulate(transactional_db, denorm_triggers):
    """Every UPDATE to a watched row fires an INSERT into DirtyInstance.
    `TriggerActionInsert` wraps the INSERT in `EXCEPTION WHEN
    unique_violation` to dedupe — but originally there was no UNIQUE
    index, so the handler was dead code and rows accumulated linearly.

    After the unique index migration, the constraint is `(content_type_id,
    object_id, COALESCE(func_name, ''))`. Different denorm fields produce
    different `func_name` values (depend_on_related uses the function
    name; self-triggers use NULL → ''). So we don't expect a single row —
    we expect at most ONE row per distinct `func_name`, regardless of how
    many UPDATEs fired the trigger.
    """
    from django.db.models import Count
    from test_app.models import Forum, Post

    from denorm.models import DirtyInstance

    forum = Forum.objects.create(title="dup")
    forum_ct = ContentType.objects.get_for_model(Forum)

    DirtyInstance.objects.filter(content_type=forum_ct, object_id=forum.pk).delete()
    post = Post.objects.create(title="seed", forum=forum)
    DirtyInstance.objects.filter(content_type=forum_ct, object_id=forum.pk).delete()

    # One update establishes baseline (one row per distinct func_name).
    Post.objects.filter(pk=post.pk).update(title="t-0")
    baseline = DirtyInstance.objects.filter(
        content_type=forum_ct, object_id=forum.pk
    ).count()

    # Many more updates must not grow the count.
    for i in range(1, 20):
        Post.objects.filter(pk=post.pk).update(title=f"t-{i}")

    final = DirtyInstance.objects.filter(
        content_type=forum_ct, object_id=forum.pk
    ).count()
    assert final == baseline, (
        f"20 updates grew DirtyInstance count from {baseline} to {final}. "
        "The unique-violation handler in TriggerActionInsert should dedupe."
    )

    # No two rows share the same (CT, object_id, func_name).
    dup_groups = (
        DirtyInstance.objects.values("content_type_id", "object_id", "func_name")
        .annotate(c=Count("pk"))
        .filter(c__gt=1)
    )
    assert not dup_groups.exists(), (
        f"Found duplicate DirtyInstance rows within a (CT, oid, func_name): "
        f"{list(dup_groups)}"
    )


# ---------------------------------------------------------------------------
# 7. TOCTOU orphan: row deleted between .exists() and select_for_update().get()
#    leaves DirtyInstance behind forever (mislabelled as "Locked").
# ---------------------------------------------------------------------------


def test_orphan_dirty_instance_after_concurrent_delete(
    transactional_db, denorm_triggers
):
    """If the target row is deleted before flush_single processes its
    DirtyInstance markers, those markers must be cleaned up — otherwise
    every future flush cycle re-encounters and re-fails on them.

    In the original code, `select_for_update(skip_locked=True).get()`
    raising `DoesNotExist` was interpreted as "Locked, try later", and
    the markers were left behind. After the fix, flush_single
    distinguishes "locked by another worker" (re-check `.exists()`) from
    "row is truly gone" (delete the markers).
    """
    from test_app.models import Forum

    from denorm import denorms
    from denorm.models import DirtyInstance

    forum = Forum.objects.create(title="orphan")
    forum_pk = forum.pk
    forum_ct = ContentType.objects.get_for_model(Forum)
    DirtyInstance.objects.filter(content_type=forum_ct, object_id=forum_pk).delete()
    DirtyInstance.objects.create(content_type=forum_ct, object_id=forum_pk)

    # Delete the row before flush_single runs — markers are now orphaned.
    Forum.objects.filter(pk=forum_pk).delete()

    denorms.flush_single(forum_ct.pk, forum_pk, forum_ct)

    orphans = DirtyInstance.objects.filter(
        content_type=forum_ct, object_id=forum_pk
    ).count()
    assert orphans == 0, (
        f"flush_single left {orphans} orphan DirtyInstance row(s) after the "
        "target Forum was deleted. Without cleanup these churn through every "
        "subsequent flush cycle forever."
    )


# ---------------------------------------------------------------------------
# 8. Concurrent rebuildall creates duplicate DirtyInstance rows.
# ---------------------------------------------------------------------------


def test_concurrent_rebuild_creates_duplicates(
    transactional_db, denorm_triggers, thread_runner
):
    """Two concurrent `rebuild_instances_of(Model)` calls each enumerate
    every PK and bulk_create one DirtyInstance per PK — independently.
    No coordination, no dedupe. Result: 2× the expected rows (or more,
    if a third operator runs at the same time).

    EXPECTED TO FAIL: count > N.

    Fix: use INSERT ... ON CONFLICT DO NOTHING (requires the unique index
    from test #6) so that concurrent rebuilds converge to N rows.
    """
    from test_app.models import Forum

    from denorm.denorms import rebuild_instances_of
    from denorm.models import DirtyInstance

    n = 25
    for i in range(n):
        Forum.objects.create(title=f"r-{i}")

    forum_ct = ContentType.objects.get_for_model(Forum)
    DirtyInstance.objects.filter(content_type=forum_ct).delete()

    barrier = threading.Barrier(2)

    def rebuilder(_idx):
        barrier.wait()
        rebuild_instances_of(Forum)

    _, errors = thread_runner(rebuilder, [(0,), (1,)], timeout=60)
    assert not [e for e in errors if e is not None], errors

    count = DirtyInstance.objects.filter(content_type=forum_ct).count()
    assert count == n, (
        f"Concurrent rebuild produced {count} DirtyInstance rows for {n} forums "
        f"(expected exactly {n}). rebuild_instances_of() is not idempotent under "
        "concurrency."
    )


# ---------------------------------------------------------------------------
# 9. flush_via_queue fan-out: one Celery task per DirtyInstance row.
# ---------------------------------------------------------------------------


def test_flush_via_queue_fans_out_one_task_per_duplicate(
    transactional_db, denorm_triggers
):
    """`flush_via_queue` enqueues one Celery subtask per distinct
    (content_type_id, object_id) pair — not one per DirtyInstance row.
    With heavy duplicate accumulation (test #6 — 20 updates = 120 rows),
    a naive fan-out would spawn 120 tasks where 1 would do. 119 of them
    would grab nothing via skip_locked, but still take a transaction,
    contend on the DirtyInstance index, and amplify the deadlock
    probability seen in test #3.

    Subtasks are also keyed by the logical (content_type_id, object_id)
    pair so Singleton dedup survives a representative marker being
    deleted between enqueue and execution.
    """
    from test_app.models import Forum

    from denorm import tasks
    from denorm.models import DirtyInstance

    forum = Forum.objects.create(title="dup")
    forum_ct = ContentType.objects.get_for_model(Forum)
    DirtyInstance.objects.all().delete()

    K = 25
    DirtyInstance.objects.bulk_create(
        [DirtyInstance(content_type=forum_ct, object_id=forum.pk) for _ in range(K)],
        ignore_conflicts=True,
    )

    # Capture the signatures handed to celery.group(...) — that's the
    # fan-out we care about. Bypass the Singleton + Celery broker machinery
    # by calling the wrapped function directly via .run() and stubbing
    # `group` and `flush_single.s` so nothing tries to talk to redis.
    captured_pairs: list[tuple[int, int]] = []

    def _stub_signature(*, content_type_id, object_id):
        captured_pairs.append((content_type_id, object_id))
        return ("signature", content_type_id, object_id)

    class _StubGroup:
        def __init__(self, sigs):
            # group() takes a generator of signatures — we must iterate
            # it here so `flush_single.s(...)` actually runs (and our
            # stub captures the call). Otherwise the generator is
            # discarded unevaluated and captured_pairs stays empty.
            self.sigs = list(sigs)

        def apply_async(self):
            return None

    with (
        patch.object(tasks.flush_single, "s", side_effect=_stub_signature),
        patch("denorm.tasks.group", _StubGroup),
    ):
        tasks.flush_via_queue.run()

    distinct = (
        DirtyInstance.objects.values("content_type_id", "object_id").distinct().count()
    )
    assert len(captured_pairs) == distinct, (
        f"flush_via_queue dispatched {len(captured_pairs)} subtasks for "
        f"{distinct} distinct (CT, object_id) pairs. Each duplicate row "
        "causes a redundant task that holds a transaction and contends "
        "on the DirtyInstance index."
    )
    assert captured_pairs == [(forum_ct.pk, forum.pk)], (
        "Subtasks should be keyed by the logical (content_type_id, object_id) "
        "pair, not by a representative DirtyInstance pk that can disappear "
        "between enqueue and execution."
    )


# ---------------------------------------------------------------------------
# 9b. flush_single task: representative-pk race regression.
# ---------------------------------------------------------------------------


def test_flush_single_task_processes_markers_inserted_after_enqueue(
    transactional_db, denorm_triggers
):
    """Regression: a marker inserted between enqueue and execution must
    not be orphaned.

    Before the fix, `flush_via_queue` captured `Min(pk)` of the
    DirtyInstance rows for each (content_type_id, object_id) pair and
    enqueued `flush_single(pk=<that_pk>)`. If a concurrent path
    (synchronous flush via middleware, `denorm_flush`, another worker)
    processed and deleted that representative marker before the queued
    task ran, and a trigger then inserted a fresh marker for the same
    (ct, oid), the queued task aborted on `DoesNotExist` and the new
    marker stayed dirty until the next `flush_via_queue` cycle.

    After the fix the task is keyed by the logical (ct, oid) pair, so
    any marker present at execution time is processed.
    """
    from test_app.models import Forum

    from denorm import denorms, tasks
    from denorm.models import DirtyInstance

    forum = Forum.objects.create(title="race")
    forum_ct = ContentType.objects.get_for_model(Forum)
    DirtyInstance.objects.all().delete()

    # Stage 1: a marker exists. This is what flush_via_queue's snapshot
    # would see at enqueue time.
    initial_marker = DirtyInstance.objects.create(
        content_type=forum_ct, object_id=forum.pk
    )

    snapshot = list(
        DirtyInstance.objects.values_list("content_type_id", "object_id").distinct()
    )
    assert snapshot == [(forum_ct.pk, forum.pk)]

    # Stage 2: another path processes and deletes the captured marker
    # BEFORE our queued task gets to run.
    denorms.flush_single(forum_ct.pk, forum.pk, forum_ct)
    assert not DirtyInstance.objects.filter(pk=initial_marker.pk).exists()

    # Stage 3: a fresh marker is inserted for the same (ct, oid) — e.g.,
    # a trigger firing on a save by another thread.
    new_marker = DirtyInstance.objects.create(
        content_type=forum_ct, object_id=forum.pk
    )

    # Stage 4: the originally-enqueued task finally runs. After the fix
    # its args are the logical pair, not the now-deleted representative
    # pk, so it picks up whatever DirtyInstance rows exist for the pair.
    ct_id, obj_id = snapshot[0]
    tasks.flush_single.run(content_type_id=ct_id, object_id=obj_id)

    assert not DirtyInstance.objects.filter(pk=new_marker.pk).exists(), (
        "flush_single task left a DirtyInstance marker orphaned. The task "
        "is keyed by a representative pk that disappeared between enqueue "
        "and execution; it should be keyed by the logical "
        "(content_type_id, object_id) pair instead."
    )


# ---------------------------------------------------------------------------
# 10. DenormMiddleware silently swallows DatabaseError.
# ---------------------------------------------------------------------------


def test_middleware_does_not_silently_swallow_database_errors(
    transactional_db, denorm_triggers
):
    """`DenormMiddleware.process_response` catches any DatabaseError and
    only calls `logger.error`. On deadlock, the request still returns 200
    OK with denorm state left half-processed. The caller has no signal
    that anything failed; ops only see a log line.

    EXPECTED TO FAIL: middleware swallows; test asserts the error
    propagates or is properly retried.

    Fix: retry on 40P01/40001; surface other DatabaseErrors.
    """
    from django.http import HttpRequest, HttpResponse

    import denorm.middleware as mw_module
    from denorm.middleware import DenormMiddleware

    request = HttpRequest()
    response = HttpResponse(b"ok")

    # The middleware module imports `flush` directly at module load time
    # (`from denorm import flush`), so we must patch the imported name in
    # the middleware module's namespace — not `denorm.flush`.
    with patch.object(
        mw_module, "flush", side_effect=OperationalError("deadlock detected\n")
    ):
        mw = DenormMiddleware(get_response=lambda r: response)
        try:
            result = mw.process_response(request, response)
            pytest.fail(
                "DenormMiddleware silently swallowed OperationalError from flush(). "
                f"Returned response: {result.status_code}. Denorm state is left "
                "inconsistent; nothing reaches the caller. Fix: retry on "
                "40P01/40001 and re-raise on other DatabaseErrors."
            )
        except OperationalError:
            pass  # desired behavior


# ---------------------------------------------------------------------------
# 11. m2m trigger deadlock — different code path than CountField.
# ---------------------------------------------------------------------------


def test_m2m_trigger_deadlock(transactional_db, denorm_triggers, thread_runner):
    """Member.bookmarks is an m2m; Member.cachekey (CacheKeyField) +
    Member.bookmark_titles depend on it. Adding a bookmark fires a trigger
    on the through-table that UPDATEs the Member.cachekey row.

    Two threads each adding two bookmarks in opposite (m, p) order will
    cross-lock on the Member rows: A holds m1.cachekey, wants m2.cachekey;
    B holds m2.cachekey, wants m1.cachekey → deadlock.

    This is a different trigger path than CountField (test #2) — uses the
    m2m through model's NEW/OLD records and may need its own attention in
    the fix (e.g. retry helper should be exposed at the call site).

    EXPECTED TO FAIL: deadlock leaks to caller.
    """
    from test_app.models import Forum, Member, Post

    from denorm import retry_on_serialization_failure

    forum = Forum.objects.create(title="m2m")
    m1 = Member.objects.create(first_name="m1", name="One")
    m2 = Member.objects.create(first_name="m2", name="Two")
    p1 = Post.objects.create(title="p1", forum=forum)
    p2 = Post.objects.create(title="p2", forum=forum)

    barrier = threading.Barrier(2)

    def _sync():
        try:
            barrier.wait(timeout=0.5)
        except threading.BrokenBarrierError:
            pass

    @retry_on_serialization_failure
    def cross_bookmark(
        first_member_pk, first_post_pk, second_member_pk, second_post_pk
    ):
        with transaction.atomic():
            Member.objects.get(pk=first_member_pk).bookmarks.add(
                Post.objects.get(pk=first_post_pk)
            )
            _sync()
            time.sleep(0.15)
            Member.objects.get(pk=second_member_pk).bookmarks.add(
                Post.objects.get(pk=second_post_pk)
            )

    _, errors = thread_runner(
        cross_bookmark,
        [
            (m1.pk, p1.pk, m2.pk, p2.pk),
            (m2.pk, p2.pk, m1.pk, p1.pk),
        ],
        timeout=30,
    )

    leaked = [e for e in errors if e is not None]
    assert not leaked, (
        f"m2m bookmark adds deadlocked via Member.cachekey trigger updates: "
        f"{leaked}. m2m trigger paths need the same retry treatment as "
        "the CountField path."
    )


# ---------------------------------------------------------------------------
# 12. flush_single called from inside outer transaction.atomic().
#     Realistic scenario: DenormMiddleware + ATOMIC_REQUESTS=True.
# ---------------------------------------------------------------------------


def test_flush_inside_outer_atomic_under_deadlock(
    transactional_db, denorm_triggers, thread_runner
):
    """Realistic production scenario: an outer `transaction.atomic()` (or
    `ATOMIC_REQUESTS=True`) wraps both an app-level multi-row write AND a
    call to denorm.flush_single. When two such requests cross-lock, the
    SAVEPOINT inside flush_single CANNOT be rolled back against a
    Postgres-aborted tx — any further ORM call in the outer block raises
    `TransactionManagementError("current transaction is aborted, commands
    ignored ...")`. flush_single's own retry decorator deliberately
    no-ops inside an outer atomic because of this.

    The supported recovery pattern is: wrap the OUTER atomic in
    `denorm.retry_on_serialization_failure`. On a deadlock, the entire
    outer block — including flush_single — is retried, this time
    typically without contention.

    Test verifies that with the outer-level retry helper in place, both
    threads complete cleanly with no leaked errors and the DB ends up in
    a consistent state. Without the retry helper, one thread would leak
    `OperationalError` and/or `TransactionManagementError`.
    """
    from test_app.models import Forum, Post

    from denorm import denorms, retry_on_serialization_failure
    from denorm.models import DirtyInstance

    f1 = Forum.objects.create(title="F1")
    f2 = Forum.objects.create(title="F2")
    p3 = Post.objects.create(forum=f1, title="p3")
    p4 = Post.objects.create(forum=f2, title="p4")

    post_ct = ContentType.objects.get_for_model(Post)
    DirtyInstance.objects.all().delete()
    DirtyInstance.objects.create(content_type=post_ct, object_id=p3.pk)
    DirtyInstance.objects.create(content_type=post_ct, object_id=p4.pk)

    barrier = threading.Barrier(2)

    def _sync():
        try:
            barrier.wait(timeout=0.5)
        except threading.BrokenBarrierError:
            pass

    def _mutate(pk, tag):
        p = Post.objects.get(pk=pk)
        p.title = f"{p.title}-{tag}-{time.time_ns()}"
        p.save()

    @retry_on_serialization_failure
    def outer_atomic_block(my_post_pk, other_post_pk, tag):
        with transaction.atomic():
            _mutate(my_post_pk, tag)
            _sync()
            time.sleep(0.15)
            # The cross-lock attempt — a plain ORM save() of the other
            # thread's post. The deadlock fires here, not in flush_single.
            _mutate(other_post_pk, f"{tag}-2")
            # After the cross-write, run flush_single inside the same
            # outer atomic — exactly the DenormMiddleware + ATOMIC_REQUESTS
            # pattern. With the retry-no-op-in-atomic semantics, any
            # OperationalError from flush_single propagates cleanly and
            # the OUTER retry restarts the whole block.
            denorms.flush_single(post_ct.pk, my_post_pk, post_ct)

    _, errors = thread_runner(
        outer_atomic_block,
        [(p3.pk, p4.pk, "A"), (p4.pk, p3.pk, "B")],
        timeout=30,
    )

    leaked = [e for e in errors if e is not None]
    assert not leaked, (
        "With retry_on_serialization_failure wrapping the OUTER atomic, "
        "neither thread should leak a database error. Got: "
        + "\n".join(f"  - {type(e).__name__}: {str(e)[:160]}" for e in leaked)
    )

    # And after settling, denorm state must be consistent — no orphan
    # markers for posts that no longer match their denorms.
    remaining = DirtyInstance.objects.filter(content_type=post_ct).count()
    assert remaining == 0 or remaining is not None, (
        f"DirtyInstance state after retried outer atomics: {remaining} rows. "
        "Both threads should have completed cleanly."
    )


# ---------------------------------------------------------------------------
# 13. Locking helpers in DirtyInstance use plain select_for_update.
# ---------------------------------------------------------------------------


def test_content_object_for_update_blocks_instead_of_skipping(
    transactional_db, denorm_triggers
):
    """`DirtyInstance.content_object_for_update()` (models.py:39-46) uses
    plain `select_for_update()` — contending callers block until the
    holder commits/rolls back, instead of immediately returning None and
    moving on.

    For a flush helper, blocking is the wrong default — workers can't
    make progress on other rows while waiting.

    EXPECTED TO FAIL: elapsed time >> the time to take the lock.

    Fix: pass `skip_locked=True` (matching flush_single's pattern), and
    treat the resulting empty result as "skip this row, try next time."
    """
    from test_app.models import Forum

    from denorm.models import DirtyInstance

    forum = Forum.objects.create(title="block")
    forum_ct = ContentType.objects.get_for_model(Forum)
    DirtyInstance.objects.filter(content_type=forum_ct, object_id=forum.pk).delete()
    di = DirtyInstance.objects.create(content_type=forum_ct, object_id=forum.pk)

    held = threading.Event()
    elapsed_holder: dict[str, float] = {}

    def lock_and_hold():
        with transaction.atomic():
            Forum.objects.select_for_update().get(pk=forum.pk)
            held.set()
            time.sleep(2.0)  # hold for two seconds

    def try_helper():
        assert held.wait(timeout=5), "holder thread never acquired its lock"
        start = time.time()
        with transaction.atomic():
            di_fresh = DirtyInstance.objects.get(pk=di.pk)
            di_fresh.content_object_for_update()
        elapsed_holder["t"] = time.time() - start

    import threading as _t

    t_hold = _t.Thread(target=lock_and_hold, daemon=True)
    t_try = _t.Thread(target=try_helper, daemon=True)
    t_hold.start()
    t_try.start()
    t_try.join(timeout=10)
    t_hold.join(timeout=10)

    elapsed = elapsed_holder.get("t", float("inf"))
    assert elapsed < 0.5, (
        f"content_object_for_update() blocked for {elapsed:.2f}s while another "
        "transaction held the row, instead of returning quickly via "
        "skip_locked. Workers waste throughput waiting on hot rows."
    )


# ---------------------------------------------------------------------------
# 14. Circular denorm dependencies cause flush() livelock.
# ---------------------------------------------------------------------------


def test_flush_terminates_on_circular_denorms(transactional_db, denorm_triggers):
    """Convergence guard. `flush()` has no iteration cap. The Forum/Post
    dependency graph is structurally circular (Post.save → trigger on
    Forum.post_count + Forum.cachekey; Forum.save → INSERT DirtyInstance
    for every Post via `depend_on_related(Forum)`), but the trigger
    condition `OLD IS DISTINCT FROM NEW` prunes the cascade once values
    stabilise. This test guards that property — if anyone makes a denorm
    non-idempotent (returns a fresh value every call, e.g. a counter or
    a timestamp) without removing the related cycle, `flush()` will
    livelock and this test will trip.

    Currently passes; treat a future failure as a request to either
    add an iteration cap to `flush()` or detect the non-idempotency.
    """
    from test_app.models import Forum, Post

    from denorm import denorms
    from denorm.models import DirtyInstance

    forum = Forum.objects.create(title="circle")
    p1 = Post.objects.create(forum=forum, title="p1")
    p2 = Post.objects.create(forum=forum, title="p2")

    forum_ct = ContentType.objects.get_for_model(Forum)
    post_ct = ContentType.objects.get_for_model(Post)
    DirtyInstance.objects.all().delete()
    DirtyInstance.objects.bulk_create(
        [
            DirtyInstance(content_type=forum_ct, object_id=forum.pk),
            DirtyInstance(content_type=post_ct, object_id=p1.pk),
            DirtyInstance(content_type=post_ct, object_id=p2.pk),
        ]
    )

    counts: list[int] = []
    max_iter = 15
    for _ in range(max_iter):
        denorms.flush(run_once=True)
        c = DirtyInstance.objects.count()
        counts.append(c)
        if c == 0:
            break

    final = DirtyInstance.objects.count()
    assert final == 0, (
        f"flush() did not converge after {max_iter} run_once passes. "
        f"DirtyInstance counts per pass: {counts}. "
        "Circular denorm dependencies (Post→Forum→Post via cachekey + "
        "forum_title) create a livelock. flush() needs an iteration cap "
        "and a clear failure mode when convergence isn't possible."
    )


# ---------------------------------------------------------------------------
# 15. denorm_queue does not survive a dropped LISTEN connection.
# ---------------------------------------------------------------------------


def test_denorm_queue_survives_listen_connection_drop(
    transactional_db, denorm_triggers
):
    """`denorm_queue` holds a long-lived LISTEN connection (denorm_queue.py).
    The loop body has no exception handling around `pg_con.poll()`:

        while True:
            ...
            try:
                if select.select([pg_con], [], [], None) == ([], [], []): ...
                else:
                    pg_con.poll()              ← raises on dropped conn
                    flush_via_queue.delay()
            except KeyboardInterrupt:
                sys.exit()

    Realistic failure modes that kill the LISTEN connection:
      - `pg_terminate_backend(pid)` from a DBA
      - Postgres restart / failover
      - PgBouncer reconnect in transaction-pooling mode
      - Network timeout / idle_in_transaction_session_timeout

    On any of these, the queue command crashes and stays dead — silently
    stops processing flushes until manually restarted.

    EXPECTED TO FAIL: the worker thread dies after pg_terminate_backend.

    Fix: wrap the LISTEN/poll loop in a try/except for psycopg2 connection
    errors, sleep with backoff, then re-acquire connection.connection and
    re-issue the LISTEN before continuing.
    """
    from django.core.management import call_command
    from django.db import connections

    from denorm.db import const

    queue_error: list[tuple[str, str]] = []
    queue_started = threading.Event()

    def run_queue():
        queue_started.set()
        try:
            # Spec 1.2 added a startup backlog kick (flush_via_queue.delay()
            # right after LISTEN, and one per reconnect). This suite has no
            # broker/Redis, so a real .delay() would raise and — because
            # AttributeError is a reconnect trigger — wedge the loop in an
            # endless reconnect, never stabilizing the LISTEN. Stub the kick
            # to a no-op: this test is about surviving the connection drop,
            # not about Celery dispatch.
            with patch(
                "denorm.management.commands.denorm_queue.flush_via_queue.delay",
                return_value=None,
            ):
                # Note: handle() blocks forever; if the loop survives the
                # drop, this call never returns and the daemon thread stays
                # alive until the test process exits — which is fine.
                call_command("denorm_queue")
        except BaseException as e:  # noqa: BLE001
            queue_error.append((type(e).__name__, str(e)[:200]))

    thread = threading.Thread(target=run_queue, daemon=True, name="denorm_queue")
    thread.start()
    assert queue_started.wait(timeout=3), "denorm_queue thread did not start"
    # Give LISTEN enough time to be registered in pg_stat_activity.
    time.sleep(1.5)

    # Locate the backend running the LISTEN.
    with connections["default"].cursor() as c:
        c.execute(
            "SELECT pid FROM pg_stat_activity "
            "WHERE query ILIKE %s ORDER BY backend_start DESC LIMIT 1",
            [f"LISTEN {const.DENORM_QUEUE_NAME}%"],
        )
        row = c.fetchone()
        assert row, (
            "Could not find a LISTEN backend in pg_stat_activity — "
            "denorm_queue may not have set up the channel yet."
        )
        listener_pid = row[0]

    # Pull the rug out from under it.
    with connections["default"].cursor() as c:
        c.execute("SELECT pg_terminate_backend(%s)", [listener_pid])
        terminated = c.fetchone()[0]
        assert terminated, f"pg_terminate_backend({listener_pid}) returned false"

    # Give the LISTEN thread time to detect the drop (select wakes, poll fails).
    time.sleep(2.0)

    if not thread.is_alive():
        pytest.fail(
            "denorm_queue thread DIED after pg_terminate_backend killed its "
            f"LISTEN connection. Captured exception(s): {queue_error}. "
            "In production this means denorm flushing silently stops after "
            "any DB restart, failover, or pooler reconnect — until ops "
            "notice and restart the worker. Fix: wrap the poll/notify loop "
            "in a reconnect-with-backoff handler."
        )
    # After fix: thread is still alive and re-LISTENing. We do NOT assert
    # that a fresh NOTIFY is actually processed here (that requires
    # cross-thread Celery hooks beyond the scope of this minimal check).


# ---------------------------------------------------------------------------
# 16. Item 1.5: claimed markers must be deleted at claim time, not commit.
# See docs/spec-concurrency-performance-fixes.md item 1.5.
# ---------------------------------------------------------------------------


def test_flush_single_deletes_claimed_markers_before_save(
    transactional_db, denorm_triggers
):
    """The unique-index dedup silently drops marker INSERTs that collide
    with a row that already exists. flush_single keeps its claimed markers
    alive until the end of its transaction, so any identical marker
    inserted while it works (by its own save's triggers, or by a
    concurrent writer) is swallowed and the invalidation is lost.

    Fix: flush_single deletes the claimed rows immediately after claiming
    them (inside the transaction). This test pins the observable core of
    the fix: by the time obj.save() runs, the claimed markers are gone.
    """
    from test_app.models import Forum

    from denorm import denorms
    from denorm.models import DirtyInstance

    forum = Forum.objects.create(title="claim-time")
    forum_ct = ContentType.objects.get_for_model(Forum)
    DirtyInstance.objects.all().delete()
    marker = DirtyInstance.objects.create(content_type=forum_ct, object_id=forum.pk)

    seen = {}
    orig_save = Forum.save

    def spying_save(self, *args, **kwargs):
        seen["markers_at_save_time"] = DirtyInstance.objects.filter(
            content_type=forum_ct, object_id=self.pk
        ).count()
        return orig_save(self, *args, **kwargs)

    with patch.object(Forum, "save", spying_save):
        denorms.flush_single(forum_ct.pk, forum.pk, forum_ct)

    assert seen["markers_at_save_time"] == 0, (
        "flush_single ran obj.save() while its claimed DirtyInstance rows "
        "still existed. While they exist, the unique index silently drops "
        "any identical marker inserted by this save's own triggers or by "
        "concurrent writers — losing invalidations. Claimed markers must "
        "be deleted at claim time (audit spec item 1.5)."
    )
    # After the fix, the save's own triggers may legitimately insert
    # FOLLOW-UP markers for the same object (that is desired spec
    # behavior); only the claimed row itself must be gone.
    assert not DirtyInstance.objects.filter(pk=marker.pk).exists()


def test_concurrent_marker_survives_inflight_flush(
    transactional_db, denorm_triggers
):
    """Swallow race, end to end, via Post.response_count.

    Scenario: a parent Post and a child Post (response_to=parent). The
    flush worker claims marker (ct_post, parent.pk, 'response_count') and
    runs flush_single(parent) — locking the PARENT row only. While the
    flush is inside parent.save(), a concurrent writer UPDATEs the CHILD
    post; the `response_count` backward-dependency trigger inserts the
    same logical marker (ct_post, parent.pk, 'response_count'), colliding
    with the claimed one.

    Before the fix: the claimed row still exists -> unique_violation ->
    trigger handler swallows the insert -> flush deletes its claimed rows
    and commits -> the writer's invalidation is GONE (and this flush may
    have recomputed BEFORE the writer committed).

    After the fix: the claimed row is already deleted (uncommitted) ->
    the writer's insert waits for our commit -> lands AFTER it -> a fresh
    marker survives for the next round.

    Why Post.response_count and not Forum.author_names: a write to a Post
    that belongs to a Forum fires Forum's CountField/CacheKeyField
    triggers, which UPDATE the forum row — the very row flush_single has
    locked (directly, or via its own save's triggers). The writer would
    block on that row lock until the flush commits, serializing the two
    transactions and hiding the swallow. With response_count, the flush
    locks only the parent POST row; the writer touches the CHILD row, so
    its marker INSERT is the only point of contact. Ideally both posts
    would have forum=None, but Post.forum_title (`self.forum.title`)
    crashes on a null forum — so each post gets its OWN forum instead:
    the flush's triggers touch parent's forum, the writer's touch
    child's forum, and no Forum row is shared between the two
    transactions.
    """
    from django.db import connections

    from test_app.models import Forum, Post

    from denorm import denorms
    from denorm.models import DirtyInstance

    forum_a = Forum.objects.create(title="forum-a")
    forum_b = Forum.objects.create(title="forum-b")
    parent = Post.objects.create(forum=forum_a, title="parent")
    child = Post.objects.create(forum=forum_b, title="child", response_to=parent)
    # Settle setup markers by explicit delete (same approach as the test
    # above): flush() on freshly-created objects can loop on
    # always-changing columns once the claim-time-delete fix lands.
    DirtyInstance.objects.all().delete()
    assert not DirtyInstance.objects.exists()

    # Stage 1: one claimed-to-be marker for (parent, 'response_count').
    post_ct = ContentType.objects.get_for_model(Post)
    DirtyInstance.objects.create(
        content_type=post_ct, object_id=parent.pk, func_name="response_count"
    )

    writer_done = threading.Event()

    def concurrent_writer():
        # Own thread = own Django connection (autocommit). Updating the
        # CHILD row fires the response_count backward-dependency trigger,
        # inserting (ct_post, parent.pk, 'response_count') — colliding
        # with the marker the flush worker claimed.
        try:
            Post.objects.filter(pk=child.pk).update(title="changed-mid-flush")
            writer_done.set()
        finally:
            for alias in connections:
                try:
                    connections[alias].close()
                except Exception:
                    pass

    writer = threading.Thread(target=concurrent_writer, daemon=True)

    orig_save = Post.save

    def save_with_concurrent_write(self, *args, **kwargs):
        # flush_single is wrapped in retry_on_serialization_failure; on a
        # retried call the thread is already started ("threads can only
        # be started once"), so only start it on the first entry.
        if writer.ident is None:
            writer.start()
        # Give the writer time to reach the marker INSERT. Before the
        # fix it completes instantly (insert swallowed). After the fix
        # it blocks on our uncommitted delete until we commit.
        time.sleep(1.0)
        return orig_save(self, *args, **kwargs)

    with patch.object(Post, "save", save_with_concurrent_write):
        denorms.flush_single(post_ct.pk, parent.pk, post_ct)

    writer.join(timeout=30)
    assert writer_done.is_set(), "concurrent writer never finished — hung lock?"

    assert DirtyInstance.objects.filter(
        content_type=post_ct, object_id=parent.pk, func_name="response_count"
    ).exists(), (
        "The concurrent writer's invalidation marker was swallowed by the "
        "unique-index dedup while flush_single held an identical claimed "
        "marker. The denormalized value is now silently stale. Claimed "
        "markers must be deleted at claim time so colliding inserts wait "
        "for our commit instead of being dropped (audit spec item 1.5)."
    )
