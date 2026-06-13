"""Queue self-convergence + flush-internal NOTIFY suppression (audit #2).

These tests prove:

A. ``flush_via_queue`` self-converges across a CROSS-OBJECT cascade using a
   Celery chord whose callback re-dispatches the flush until the dirty table
   is empty — WITHOUT relying on the (now suppressed) flush-internal NOTIFY.
B. flush-internal marker INSERTs no longer NOTIFY the queue channel (a session
   GUC ``denorm.flushing`` set with SET LOCAL guards the trigger), while genuine
   ORM writes still NOTIFY.

Run with:
    uv run pytest tests/test_queue_convergence.py -v
"""

from __future__ import annotations

import select
import time

import pytest
from django.contrib.contenttypes.models import ContentType
from django.db import connection

from denorm.db import const

# ---------------------------------------------------------------------------
# 1. Queue self-converges across a CROSS-OBJECT cascade, NOTIFY suppressed.
# ---------------------------------------------------------------------------


def test_flush_via_queue_self_converges_cross_object_cascade(
    transactional_db, denorm_triggers, live_worker
):
    """The critical test.

    Post.response_count @depend_on_related("self", type="backward"): a child
    Post's recompute cascades to its PARENT (a DIFFERENT object) via the
    backward dependency trigger. We dirty ONLY the child so the parent is NOT
    in the initial flush_via_queue snapshot. With a real worker (eager OFF) we
    submit ONE flush_via_queue. The chord callback must re-discover the parent
    marker created during the child's flush and re-dispatch until the dirty
    table drains — proving self-convergence WITHOUT relying on flush-internal
    NOTIFY (which is suppressed by the GUC).
    """
    from test_app.models import Forum, Post

    from denorm import denorms, tasks
    from denorm.models import DirtyInstance

    forum = Forum.objects.create(title="conv")
    parent = Post.objects.create(forum=forum, title="parent")
    child = Post.objects.create(forum=forum, title="child", response_to=parent)
    # Settle setup, then start from a clean dirty table.
    denorms.flush()
    DirtyInstance.objects.all().delete()
    assert not DirtyInstance.objects.exists()

    parent.refresh_from_db()
    assert parent.response_count == 1  # one direct response

    # Add a grandchild so the parent's response_count must become 2. Dirty
    # ONLY the child (the grandchild's direct parent). The child's recompute
    # cascades UP to the (grand)parent — a cross-object marker that is NOT in
    # the initial snapshot and (with NOTIFY suppressed) is ONLY drained by the
    # chord re-dispatch.
    Post.objects.create(forum=forum, title="grandchild", response_to=child)
    DirtyInstance.objects.all().delete()  # clear markers from the create
    post_ct = ContentType.objects.get_for_model(Post)
    DirtyInstance.objects.create(
        content_type=post_ct, object_id=child.pk, func_name="response_count"
    )
    assert list(
        DirtyInstance.objects.values_list("content_type_id", "object_id").distinct()
    ) == [(post_ct.pk, child.pk)], "only the child must be in the initial snapshot"

    # ONE dispatch over the real broker. Self-convergence (chord re-dispatch)
    # must drain the cascade to the parent without any further kick from us.
    result = tasks.flush_via_queue.delay()
    assert type(result).__name__ != "EagerResult", (
        "expected a real (non-eager) dispatch under live_worker; got EagerResult"
    )

    deadline = time.time() + 30
    while DirtyInstance.objects.exists() and time.time() < deadline:
        time.sleep(0.2)

    assert not DirtyInstance.objects.exists(), (
        "flush_via_queue did NOT self-converge: cross-object cascade markers "
        "remain. The chord callback must re-dispatch until the dirty table is "
        "empty (NOTIFY is suppressed during flush, so the queue cannot rely on "
        "it to rediscover cross-object cascades)."
    )

    parent.refresh_from_db()
    child.refresh_from_db()
    assert child.response_count == 1, f"child.response_count={child.response_count}"
    assert parent.response_count == 2, (
        f"parent.response_count={parent.response_count}; the grandchild cascade "
        "did not reach the parent — queue did not drain the cross-object chain."
    )


# ---------------------------------------------------------------------------
# 2. Pass cap: a non-convergent flush stops at DENORM_MAX_QUEUE_PASSES.
# ---------------------------------------------------------------------------


def test_flush_via_queue_pass_cap(transactional_db, denorm_triggers, live_worker):
    """Simulate a non-convergent denorm: every flush_batch round re-inserts a
    marker. The chord loop must stop at DENORM_MAX_QUEUE_PASSES — no infinite
    chord — and log an error. Deterministic: we patch the batch unit to leave
    exactly one fresh marker each round.
    """
    from unittest.mock import patch

    from test_app.models import Forum

    from denorm import tasks
    from denorm.models import DirtyInstance

    forum = Forum.objects.create(title="cap")
    forum_ct = ContentType.objects.get_for_model(Forum)
    DirtyInstance.objects.all().delete()
    DirtyInstance.objects.create(content_type=forum_ct, object_id=forum.pk)

    call_count = {"n": 0}

    def fake_flush_single(content_type_id, object_id, *a, **kw):
        # Always-dirty: delete the claimed marker, then re-insert one so the
        # table is never empty -> the chord must re-dispatch every pass.
        call_count["n"] += 1
        DirtyInstance.objects.filter(
            content_type_id=content_type_id, object_id=object_id
        ).delete()
        DirtyInstance.objects.create(
            content_type_id=content_type_id, object_id=object_id
        )

    cap = 4
    with (
        patch("denorm.denorms.flush_single", side_effect=fake_flush_single),
        patch("denorm.conf.settings.DENORM_MAX_QUEUE_PASSES", cap),
    ):
        tasks.flush_via_queue.delay()
        # Let the bounded chord loop run to exhaustion.
        deadline = time.time() + 30
        # Once the cap is hit, no further dispatch happens, so call_count
        # stabilises. Wait until it stops growing for a short quiet period.
        last = -1
        stable_since = None
        while time.time() < deadline:
            now = call_count["n"]
            if now == last:
                if stable_since is None:
                    stable_since = time.time()
                elif time.time() - stable_since > 2.0:
                    break
            else:
                last = now
                stable_since = None
            time.sleep(0.2)

    # The chord must have stopped: bounded by the pass cap, not infinite.
    # _pass starts at 0; passes 0..cap-1 each run one flush_batch -> cap calls,
    # the pass==cap dispatch aborts. Allow a small margin for timing.
    assert call_count["n"] <= cap + 1, (
        f"flush_via_queue ran flush_single {call_count['n']} times; the chord "
        f"loop must stop at DENORM_MAX_QUEUE_PASSES={cap}."
    )
    assert call_count["n"] >= cap, (
        f"flush_via_queue only ran {call_count['n']} times; expected ~{cap} "
        "passes before the cap aborts."
    )
    # Markers remain (non-convergent) — proves it stopped, did not drain.
    assert DirtyInstance.objects.exists()


# ---------------------------------------------------------------------------
# 3. NOTIFY suppressed during flush (Part B GUC guard).
# ---------------------------------------------------------------------------


def _drain_notifies(pg_con, timeout=2.0):
    """Poll a raw psycopg2 LISTEN connection for NOTIFY messages."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if select.select([pg_con], [], [], 0.2) != ([], [], []):
            pg_con.poll()
            if pg_con.notifies:
                break
    msgs = list(pg_con.notifies)
    pg_con.notifies.clear()
    return msgs


def test_notify_suppressed_during_flush_but_not_genuine_write(
    transactional_db, denorm_triggers
):
    """A genuine ORM write fires a NOTIFY on the denorm queue channel; a
    denorms.flush() (its marker INSERTs) fires NO NOTIFY. Proves the
    ``denorm.flushing`` GUC guard in the trigger function.
    """
    from test_app.models import Forum, Post

    from denorm import denorms
    from denorm.models import DirtyInstance

    # Raw LISTEN connection on a SECOND backend (mirror the deadlock-suite
    # reconnect test). Must be its own psycopg2 connection in autocommit.
    import psycopg2

    db = connection.settings_dict
    listen_conn = psycopg2.connect(
        dbname=db["NAME"],
        user=db["USER"],
        password=db["PASSWORD"],
        host=db["HOST"],
        port=db["PORT"],
    )
    listen_conn.set_isolation_level(
        psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT
    )
    try:
        with listen_conn.cursor() as cur:
            cur.execute(f"LISTEN {const.DENORM_QUEUE_NAME};")

        # A cross-object cascade so the FLUSH itself inserts markers (for a
        # different object) — that is the marker-INSERT-during-flush whose
        # NOTIFY we must suppress. response_count: a child's recompute marks
        # its parent.
        forum = Forum.objects.create(title="notify")
        parent = Post.objects.create(forum=forum, title="parent")
        child = Post.objects.create(forum=forum, title="child", response_to=parent)
        denorms.flush()
        DirtyInstance.objects.all().delete()
        _drain_notifies(listen_conn, timeout=1.0)  # clear setup notifies

        # (a) Genuine write: creating a Post fires triggers -> marker INSERTs
        # -> NOTIFY must fire.
        Post.objects.create(forum=forum, title="gc", response_to=child)
        genuine = _drain_notifies(listen_conn, timeout=3.0)
        assert genuine, (
            "a genuine ORM write did NOT NOTIFY the denorm queue channel — "
            "the trigger must still notify for real writes."
        )

        # (b) flush(): marker INSERTs happen inside flush_single's transaction
        # with denorm.flushing='on' -> NO NOTIFY. The cross-object cascade
        # (child -> parent response_count) guarantees flush_single's own saves
        # insert fresh markers — without the GUC guard these would NOTIFY.
        assert DirtyInstance.objects.exists(), "expected markers to flush"
        denorms.flush()
        assert not DirtyInstance.objects.exists(), "flush did not drain"
        suppressed = _drain_notifies(listen_conn, timeout=3.0)
        assert suppressed == [], (
            "denorms.flush() fired a NOTIFY on the denorm queue channel; "
            "flush-internal marker INSERTs must be suppressed via the "
            "denorm.flushing GUC."
        )
    finally:
        listen_conn.close()


# ---------------------------------------------------------------------------
# 4. Inline flush() still converges and settles values (regression).
# ---------------------------------------------------------------------------


def test_inline_flush_still_converges(transactional_db, denorm_triggers):
    """Suppressing flush-internal NOTIFY must not affect inline flush(): it
    self-converges via its own outer loop and settles denorm values.
    """
    from test_app.models import Forum, Post

    from denorm import denorms
    from denorm.models import DirtyInstance

    forum = Forum.objects.create(title="inline")
    parent = Post.objects.create(forum=forum, title="parent")
    child = Post.objects.create(forum=forum, title="child", response_to=parent)
    grandchild = Post.objects.create(  # noqa: F841
        forum=forum, title="grandchild", response_to=child
    )

    denorms.flush()
    assert not DirtyInstance.objects.exists(), "inline flush did not drain"

    forum.refresh_from_db()
    parent.refresh_from_db()
    child.refresh_from_db()
    assert forum.post_count == 3
    assert parent.response_count == 2
    assert child.response_count == 1


# ---------------------------------------------------------------------------
# 5. Migration 0019 applies forward + reverse cleanly; trigger still works.
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_migration_0019_forward_and_reverse():
    """0019 (conditional NOTIFY) applies forward and reverses to the
    unconditional 0016 version; the notify function exists after each.
    """
    from django.db import connection as conn

    def func_src():
        with conn.cursor() as c:
            c.execute(
                "SELECT pg_get_functiondef(oid) FROM pg_proc "
                "WHERE proname = 'notify_django_denorm_queue'"
            )
            row = c.fetchone()
            return row[0] if row else None

    from django.db.migrations.executor import MigrationExecutor

    # Forward to 0019 (current head): the guard must be present.
    executor = MigrationExecutor(conn)
    executor.migrate([("denorm", "0019_conditional_notify_during_flush")])
    src = func_src()
    assert src is not None
    assert "denorm.flushing" in src, (
        "0019 forward did not install the conditional NOTIFY guard"
    )

    # Reverse to 0018: unconditional NOTIFY restored, no guard.
    executor = MigrationExecutor(conn)
    executor.migrate([("denorm", "0018_alter_dirtyinstance_content_type_and_more")])
    src = func_src()
    assert src is not None
    assert "denorm.flushing" not in src, (
        "0019 reverse did not restore the unconditional NOTIFY function"
    )

    # Migrate forward again so we leave the DB at head for other tests.
    executor = MigrationExecutor(conn)
    executor.migrate([("denorm", "0019_conditional_notify_during_flush")])
    assert "denorm.flushing" in func_src()
