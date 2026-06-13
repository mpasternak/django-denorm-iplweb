"""Drop redundant self-markers on ORM saves — safety boundary tests."""
from __future__ import annotations

import threading
import time

from django.contrib.contenttypes.models import ContentType
from django.db import connections, transaction


def _markers(model, pk):
    from denorm.models import DirtyInstance

    ct = ContentType.objects.get_for_model(model)
    return set(
        DirtyInstance.objects.filter(content_type=ct, object_id=pk)
        .values_list("func_name", flat=True)
    )


def test_plain_column_samemodel_marker_dropped(transactional_db, denorm_triggers):
    """full_name (depends on PLAIN columns first_name/last_name) is correct
    after a plain save -> its self-marker is redundant -> dropped in post_save."""
    from test_app.models import Profile

    p = Profile.objects.create(first_name="John", last_name="Doe")
    # create() is a full save: full_name recomputed correctly by pre_save.
    assert "full_name" not in _markers(Profile, p.pk), (
        "redundant full_name marker should be dropped in post_save"
    )
    p.refresh_from_db()
    assert p.full_name == "John Doe"


def test_chain_marker_kept(transactional_db, denorm_triggers):
    """letterhead depends on the DENORM field full_name (chain) -> order-
    sensitive -> NOT safe to drop -> marker kept for flush/convergence."""
    from test_app.models import Profile

    p = Profile.objects.create(first_name="John", last_name="Doe")
    assert "letterhead" in _markers(Profile, p.pk), (
        "chain marker must be kept (only flush/convergence may settle it)"
    )


def test_related_dep_marker_kept(transactional_db, denorm_triggers):
    """Post.forum_title @depend_on_related(Forum) -> related -> NOT dropped."""
    from test_app.models import Forum, Post
    from denorm import denorms
    from denorm.models import DirtyInstance

    forum = Forum.objects.create(title="F")
    denorms.flush()
    DirtyInstance.objects.all().delete()
    post = Post.objects.create(forum=forum, title="p")  # ORM save of Post
    assert "forum_title" in _markers(Post, post.pk), (
        "related-dependency marker must NOT be dropped "
        "(in-memory related may be stale)"
    )


def test_undeclared_marker_kept(transactional_db, denorm_triggers):
    """UndeclaredProfile.full_name has no @depend_on_fields -> unknown reads -> kept."""
    from test_app.models import UndeclaredProfile

    p = UndeclaredProfile.objects.create(first_name="A", last_name="B")
    assert "full_name" in _markers(UndeclaredProfile, p.pk)


def test_update_fields_respected(transactional_db, denorm_triggers):
    """save(update_fields=['first_name']) does NOT recompute full_name's
    column (its pre_save doesn't run) -> marker must be KEPT."""
    from test_app.models import Profile
    from denorm import denorms
    from denorm.models import DirtyInstance

    p = Profile.objects.create(first_name="John", last_name="Doe")
    denorms.flush()
    DirtyInstance.objects.all().delete()
    p.first_name = "Jane"
    p.save(update_fields=["first_name"])
    assert "full_name" in _markers(Profile, p.pk), (
        "full_name not recomputed by update_fields=['first_name'] -> marker kept"
    )
    # and a full save DOES drop it:
    DirtyInstance.objects.all().delete()
    p.last_name = "Smith"
    p.save()  # full save recomputes full_name
    assert "full_name" not in _markers(Profile, p.pk)


def test_bulk_update_marker_untouched(transactional_db, denorm_triggers):
    """QuerySet.update fires no post_save -> markers stay -> flush still settles."""
    from test_app.models import Profile
    from denorm import denorms
    from denorm.models import DirtyInstance

    p = Profile.objects.create(first_name="John", last_name="Doe")
    denorms.flush()
    DirtyInstance.objects.all().delete()
    Profile.objects.filter(pk=p.pk).update(last_name="Smith")  # bypass
    assert "full_name" in _markers(Profile, p.pk), "bulk update marker must remain"
    denorms.flush()
    p.refresh_from_db()
    assert p.full_name == "John Smith"  # flush still settles


def test_endtoend_correctness_no_stale(transactional_db, denorm_triggers):
    """With the optimization on, a full flush yields correct values everywhere."""
    from test_app.models import Profile
    from denorm import denorms

    p = Profile.objects.create(first_name="John", last_name="Doe")
    denorms.flush()
    p.refresh_from_db()
    assert p.full_name == "John Doe"
    assert p.letterhead == "Dear John Doe"
    p.first_name = "Jane"
    p.save()
    denorms.flush()
    p.refresh_from_db()
    assert p.full_name == "Jane Doe"
    assert p.letterhead == "Dear Jane Doe"


# ---------------------------------------------------------------------------
# Staged concurrency (design spec item 6): the dangerous direction.
#
# The safety of dropping a plain-column self-marker rests on ONE invariant:
# a full ORM save writes ALL the plain source columns AND the denorm column
# in a SINGLE transaction holding the row lock, so at commit the row is
# self-consistent — and the post_save DELETE of the (now redundant) marker
# runs in that same transaction. We stress that with a separate-connection
# writer (pattern: tests/test_deadlocks.py).
# ---------------------------------------------------------------------------


def test_concurrent_marker_committed_before_save_no_stale(
    transactional_db, denorm_triggers
):
    """(a) Writer COMMITS a first_name change + full_name marker BEFORE our
    full save. Our instance loaded with the OLD in-memory first_name, so our
    full save overwrites first_name back (last-write-wins) AND recomputes
    full_name consistently, then drops the marker. The row MUST be
    self-consistent immediately after our save (no stale window with no
    marker), and a later flush keeps it consistent."""
    from test_app.models import Profile

    from denorm import denorms
    from denorm.models import DirtyInstance

    p = Profile.objects.create(first_name="John", last_name="Doe")
    denorms.flush()
    DirtyInstance.objects.all().delete()

    # Load our instance NOW: in-memory first_name == 'John'.
    obj = Profile.objects.get(pk=p.pk)

    # Concurrent committed change + marker, behind our instance's back.
    Profile.objects.filter(pk=p.pk).update(first_name="WRITER")
    assert "full_name" in _markers(Profile, p.pk)

    # Our full save: overwrites first_name -> 'John', full_name -> 'John Doe...'.
    obj.last_name = "Smith"
    obj.save()

    after = Profile.objects.get(pk=p.pk)
    assert after.full_name == f"{after.first_name} {after.last_name}", (
        "row left inconsistent after full save (would be silent stale): "
        f"full={after.full_name!r} first={after.first_name!r}"
    )
    denorms.flush()
    final = Profile.objects.get(pk=p.pk)
    assert final.full_name == f"{final.first_name} {final.last_name}"


def test_concurrent_writer_serialized_after_save_marker_survives(
    transactional_db, denorm_triggers
):
    """(a') Writer's first_name change is SERIALIZED AFTER our save by the row
    lock (it blocks on our uncommitted UPDATE until we commit). Its column-
    watch trigger then inserts a FRESH full_name marker that our already-
    committed DELETE cannot eat. The row is momentarily inconsistent but
    CARRIES a marker, and flush settles it. Proves no lost invalidation."""
    from test_app.models import Profile

    from denorm import denorms
    from denorm.models import DirtyInstance

    p = Profile.objects.create(first_name="John", last_name="Doe")
    denorms.flush()
    DirtyInstance.objects.all().delete()

    go = threading.Event()
    done = threading.Event()

    def writer():
        try:
            go.wait(timeout=10)
            # Blocks on our row lock until we commit, then runs + commits.
            Profile.objects.filter(pk=p.pk).update(first_name="OTHER")
            done.set()
        finally:
            for alias in connections:
                try:
                    connections[alias].close()
                except Exception:
                    pass

    t = threading.Thread(target=writer, daemon=True)
    t.start()

    obj = Profile.objects.get(pk=p.pk)
    with transaction.atomic():
        obj.last_name = "Smith"
        obj.save()  # holds row lock; post_save drops full_name marker
        go.set()
        time.sleep(1.0)  # writer is blocked on our row lock here
    # our txn committed -> writer unblocks, its UPDATE + marker land AFTER us
    assert done.wait(timeout=10)
    t.join(timeout=10)

    after = Profile.objects.get(pk=p.pk)
    inconsistent = after.full_name != f"{after.first_name} {after.last_name}"
    if inconsistent:
        # The writer's change landed after our commit -> there MUST be a fresh
        # marker so flush can settle it. A missing marker here = stale hole.
        assert "full_name" in _markers(Profile, p.pk), (
            "HOLE: row inconsistent after concurrent write but no full_name "
            f"marker (full={after.full_name!r} first={after.first_name!r})"
        )
    denorms.flush()
    final = Profile.objects.get(pk=p.pk)
    assert final.full_name == f"{final.first_name} {final.last_name}"


def test_related_marker_never_dropped_with_concurrent_change(
    transactional_db, denorm_triggers
):
    """(b) RELATED-dep func (Post.forum_title): even with a concurrent
    committed change to the parent forum, an ORM save of the Post must NEVER
    drop forum_title's marker; flush re-reads fresh DB state."""
    from test_app.models import Forum, Post

    from denorm import denorms
    from denorm.models import DirtyInstance

    forum = Forum.objects.create(title="Orig")
    post = Post.objects.create(forum=forum, title="p")
    denorms.flush()
    DirtyInstance.objects.all().delete()

    # Concurrent committed change to the forum -> marks post.forum_title.
    Forum.objects.filter(pk=forum.pk).update(title="Changed")

    # An ORM save of the Post (in-memory forum is the stale 'Orig').
    post = Post.objects.get(pk=post.pk)
    post.title = "p2"
    post.save()

    assert "forum_title" in _markers(Post, post.pk), (
        "related-dependency marker must survive an ORM save of the Post"
    )
    denorms.flush()
    post.refresh_from_db()
    assert post.forum_title == "Changed"
