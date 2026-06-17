"""DirtyInstance marker claim/delete/orphan race reproducers.

Split out of the former tests/test_deadlocks.py (see that file's history).
Shared fixtures live in tests/conftest.py.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

from django.contrib.contenttypes.models import ContentType
from django.db import transaction


# ---------------------------------------------------------------------------
# 4. res.delete() race: new dirty markers inserted during flush_single
#    by a concurrent transaction get wiped along with the originals.
# ---------------------------------------------------------------------------


def test_dirty_markers_inserted_during_flush_are_not_wiped(
    transactional_db, denorm_triggers, thread_runner
):
    """A DirtyInstance row inserted by a CONCURRENT transaction during
    flush_single must NOT be silently wiped without being processed.

    Two valid outcomes (both honor the invalidation):
      1. The marker survives flush_single and is handled by a later pass
         (old single-save behavior); OR
      2. The convergence loop (spec 2.5) re-claims the now-committed marker
         and recomputes from the same committed state — the marker is gone
         BECAUSE it was serviced (an extra save ran for it), not wiped.

    The original bug (res.delete() deleting by (content_type_id, object_id)
    and wiping unclaimed rows) is excluded by both: this test asserts the
    sentinel marker was either left intact or consumed-with-recompute.

    Reproducer:
      Thread A:
        - Has DirtyInstance(Forum=F) marker
        - Starts flush_single(F): locks DirtyInstance rows, locks F row,
          calls F.save() (slow — we widen the window).
        - Thread B inserts a NEW DirtyInstance(F) during the save.

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
    save_calls = []

    def slow_save(self, *args, **kwargs):
        result = original_save(self, *args, **kwargs)
        save_calls.append(1)
        if not inside_save.is_set():
            # Only widen the window on the FIRST save (the convergence loop
            # may run additional saves to service the concurrent marker).
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
        # Either the marker survived for a later pass (1), or the convergence
        # loop consumed it (0) — in which case an EXTRA save must have run to
        # service it (proof it was recomputed, not silently wiped).
        if surviving == 0:
            assert len(save_calls) >= 2, (
                "Concurrent marker was deleted WITHOUT a recompute → silently "
                f"lost dirty marker (save_calls={len(save_calls)})."
            )
        else:
            assert surviving == 1, (
                "res.delete() wiped a DirtyInstance inserted by a CONCURRENT "
                "transaction during flush_single → silently lost dirty marker."
            )
    finally:
        Forum.save = original_save

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
