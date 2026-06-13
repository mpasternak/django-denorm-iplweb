"""Drop redundant self-markers on ORM saves — safety boundary tests."""
from __future__ import annotations

from django.contrib.contenttypes.models import ContentType


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
