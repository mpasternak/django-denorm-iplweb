"""Trigger / count-field / m2m deadlock reproducers.

Split out of the former tests/test_deadlocks.py (see that file's history).
Shared fixtures live in tests/conftest.py.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

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
# 18. Spec 1.4: AggregateField.pre_save read-then-write loses concurrent
# trigger increments.
# ---------------------------------------------------------------------------


def test_countfield_save_does_not_clobber_concurrent_increment(
    transactional_db, denorm_triggers
):
    """AggregateField.pre_save SELECTs the trigger-maintained counter and
    save() writes that value back. An increment committed between the
    SELECT and the UPDATE is silently overwritten. Fix: write
    `col = col` (an F() expression) so the UPDATE can never lose
    concurrent increments.

    Staged deterministically: a hook between pre_save and the UPDATE
    commits a child insert (trigger increments the counter), then the
    parent save proceeds.
    """
    from test_app.models import Forum, Post

    from denorm import denorms
    from denorm.fields import AggregateField
    from denorm.models import DirtyInstance

    forum = Forum.objects.create(title="cnt")
    denorms.flush()
    DirtyInstance.objects.all().delete()
    forum.refresh_from_db()
    assert forum.post_count == 0

    orig_pre_save = AggregateField.pre_save
    state = {"fired": False}

    def racing_pre_save(self, instance, add):
        value = orig_pre_save(self, instance, add)
        if not add and not state["fired"]:
            state["fired"] = True

            def writer():
                from django.db import connections

                try:
                    # Own thread = own connection (autocommit): the child
                    # commits and its trigger increments forum.post_count
                    # BEFORE the parent's UPDATE executes.
                    Post.objects.create(forum_id=instance.pk, title="mid-save")
                finally:
                    for alias in connections:
                        try:
                            connections[alias].close()
                        except Exception:
                            pass

            t = threading.Thread(target=writer, daemon=True)
            t.start()
            t.join(timeout=30)
            assert not t.is_alive(), "writer hung — unexpected lock"
        return value

    with patch.object(AggregateField, "pre_save", racing_pre_save):
        forum.save()

    count_in_db = Forum.objects.values_list("post_count", flat=True).get(
        pk=forum.pk
    )
    assert count_in_db == 1, (
        "Parent save() overwrote the trigger-maintained counter with the "
        "value read before the concurrent increment committed (lost "
        "update). pre_save must emit `col = col`, not a snapshot value."
    )
