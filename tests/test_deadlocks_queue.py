"""Celery queue-path (flush_via_queue / flush_batch / singleton) reproducers.

Split out of the former tests/test_deadlocks.py (see that file's history).
Shared fixtures live in tests/conftest.py.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

import pytest
from django.contrib.contenttypes.models import ContentType


# ---------------------------------------------------------------------------
# 9. flush_via_queue fan-out: distinct pairs dispatched in chunks via flush_batch.
# ---------------------------------------------------------------------------


def test_flush_via_queue_drains_db_through_a_real_worker(
    transactional_db, denorm_triggers, live_worker
):
    """End-to-end, eager OFF: submit flush_via_queue over a real broker and
    let an in-process worker drain the queue. Verifies the genuine async
    path (serialization, round-trip, group fan-out, flush_single), not just
    the call shape."""
    from test_app.models import Forum, Post

    from denorm import tasks
    from denorm.models import DirtyInstance

    forum = Forum.objects.create(title="q*")
    Post.objects.create(forum=forum, title="p")
    # settle setup synchronously, then dirty deterministically
    from denorm import denorms

    denorms.flush()
    DirtyInstance.objects.all().delete()
    forum_ct = ContentType.objects.get_for_model(Forum)
    # duplicate markers for one pair — must collapse to one logical flush
    DirtyInstance.objects.create(content_type=forum_ct, object_id=forum.pk)
    DirtyInstance.objects.create(
        content_type=forum_ct, object_id=forum.pk, func_name="author_names"
    )

    # One flush_via_queue dispatch does ONE fan-out pass: it groups the
    # distinct (ct, oid) pairs into flush_batch tasks, each of which runs
    # flush_single once per pair. The Forum/Post denorm graph is
    # self-dirtying (saving a Forum re-marks author_names/path/tags_string
    # and its related Posts — the same convergence that denorms.flush()
    # loops over internally), so a single pass cannot drain it. In
    # production the denorm_queue command re-kicks flush_via_queue.delay()
    # on every NOTIFY until the backlog clears; we drive that same re-kick
    # here over the real broker, asserting the queue path actually
    # converges via a genuine async round-trip (serialization, worker
    # pickup, group fan-out, flush_single), not just the call shape.
    deadline = time.time() + 30
    # First dispatch: assert we are genuinely on the non-eager (real broker)
    # path. If live_worker is absent, CELERY_TASK_ALWAYS_EAGER stays ON and
    # .delay() returns an EagerResult — the test would silently pass without
    # ever touching a real worker. Catching that here makes the premise
    # self-enforcing.
    first = tasks.flush_via_queue.delay()
    assert type(first).__name__ != "EagerResult", (
        "expected a real (non-eager) dispatch under live_worker; got EagerResult "
        "— the worker path is not being exercised"
    )
    first.get(timeout=20)
    while DirtyInstance.objects.exists() and time.time() < deadline:
        tasks.flush_via_queue.delay().get(timeout=20)  # real dispatch + drain
        time.sleep(0.2)
    assert not DirtyInstance.objects.exists(), (
        "the real worker did not drain DirtyInstance within 30s"
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
# 9c. flush_batch Singleton dedup: identical chunks share a lock key.
# ---------------------------------------------------------------------------


def test_flush_batch_singleton_dedups_identical_chunks(celery_redis):
    """Eager can't show concurrent dedup (the first task releases its lock
    before the second starts), so assert the dedup MECHANISM directly: two
    identical flush_batch chunks generate the same Singleton lock key, and
    while the lock is held a second acquire fails."""
    from denorm import tasks

    chunk = [(1, 1), (1, 2)]
    lock = tasks.flush_batch.generate_lock(tasks.flush_batch.name, [], {"pairs": chunk})
    same = tasks.flush_batch.generate_lock(tasks.flush_batch.name, [], {"pairs": chunk})
    other = tasks.flush_batch.generate_lock(
        tasks.flush_batch.name, [], {"pairs": [(1, 3)]}
    )
    assert lock == same, "identical chunks must dedup to the same lock key"
    assert lock != other, "different chunks must not collide"

    backend = tasks.flush_batch.singleton_backend
    try:
        assert backend.lock(lock, "tid-1", expiry=60) is True
        assert backend.lock(lock, "tid-2", expiry=60) is False  # held
    finally:
        backend.unlock(lock)
    assert backend.lock(lock, "tid-3", expiry=60) is True
    backend.unlock(lock)

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
            # MINIMAL stub, retained deliberately (not a leftover): the
            # celery_redis fixture now provides a real broker, but the
            # startup backlog kick (flush_via_queue.delay() right after
            # LISTEN) runs EAGERLY here, i.e. inline in this thread. Its body
            # issues Django ORM queries against connections["default"] — the
            # very connection denorm_queue has just grabbed as its raw
            # psycopg2 LISTEN socket and switched to AUTOCOMMIT isolation via
            # set_isolation_level() (behind Django's back). The eager ORM
            # query corrupts that shared connection's state and Postgres
            # closes it ("server closed the connection unexpectedly"), which
            # AttributeError/OperationalError treats as a connection drop ->
            # the loop reconnects endlessly and never registers a stable
            # LISTEN, so pg_stat_activity has no listener to terminate.
            # Stubbing the kick to a no-op isolates THIS test to its actual
            # subject — surviving a dropped LISTEN connection — not the eager
            # flush/LISTEN connection-sharing interaction (that is exercised
            # for real by the live_worker drain test above). This is a
            # harness interaction, not a denorm runtime bug.
            #
            # Note: handle() blocks forever; if the loop survives the drop,
            # this call never returns and the daemon thread stays alive until
            # the test process exits — which is fine.
            # Rebind the module-level NAME to a Mock (instead of patching
            # `.delay` on the shared task object). call_command("denorm_queue")
            # blocks forever, so the `with patch(...)` context never exits and
            # its restore never runs. Patching the real task's `.delay` would
            # therefore leak a return_value=None mock across the whole test
            # session, breaking every later test that depends on
            # flush_via_queue.delay() returning a real AsyncResult. Patching
            # the imported name leaves the real task untouched.
            with patch(
                "denorm.management.commands.denorm_queue.flush_via_queue"
            ):
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

# ---------------------------------------------------------------------------
# 17. Spec 1.3: celery-singleton locks must expire.
# ---------------------------------------------------------------------------


def test_singleton_tasks_carry_lock_expiry():
    """Without lock_expiry, a SIGKILLed worker leaves the Redis lock
    forever and that (content_type, object) pair can never be enqueued
    again — denormalization for the object silently stops."""
    from denorm import tasks
    from denorm.conf import settings as denorm_settings

    assert denorm_settings.DENORM_SINGLETON_LOCK_EXPIRY == 600
    for task in (tasks.flush_single, tasks.flush_via_queue):
        assert task.lock_expiry == denorm_settings.DENORM_SINGLETON_LOCK_EXPIRY, (
            f"{task.name} has no lock_expiry; a crashed worker permanently "
            "wedges this Singleton."
        )
