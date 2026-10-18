"""denorm.deferred(): opt-in deferral of @denormalized computation."""

from __future__ import annotations

import asyncio
import threading
from unittest import mock

import pytest
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.test import override_settings

import denorm
from denorm.deferral import deferral_active


def _markers(model, pk):
    from denorm.models import DirtyInstance

    ct = ContentType.objects.get_for_model(model)
    return set(
        DirtyInstance.objects.filter(content_type=ct, object_id=pk).values_list(
            "func_name", flat=True
        )
    )


def _settled_profile(**kw):
    from test_app.models import Profile

    from denorm import denorms
    from denorm.models import DirtyInstance

    p = Profile.objects.create(first_name="John", last_name="Doe", **kw)
    denorms.flush()
    DirtyInstance.objects.all().delete()
    p.refresh_from_db()
    assert p.full_name == "John Doe"
    return p


def _spy(model, field_name):
    """Wrap the denorm callback of ``model.field_name`` with a call counter."""
    field = model._meta.get_field(field_name)
    return mock.patch.object(field.denorm, "func", wraps=field.denorm.func)


# --- target resolution -------------------------------------------------------


def test_requires_targets():
    with pytest.raises(TypeError):
        with denorm.deferred():
            pass


@pytest.mark.parametrize(
    "target",
    [
        "test_app.Forum.post_count",  # CountField: trigger-maintained
        "test_app.Forum.cachekey",  # CacheKeyField
        "test_app.Forum.authors",  # @denormalized ManyToManyField
        "test_app.Forum.title",  # plain column
        "test_app.Tag",  # model without @denormalized fields
        "test_app",  # malformed
        42,
    ],
)
def test_rejects_invalid_targets(target):
    with pytest.raises((TypeError, ValueError)):
        with denorm.deferred(target):
            pass
    assert not deferral_active()


def test_target_forms_resolve_to_fields():
    from test_app.models import Forum, Profile

    from denorm.deferral import _current

    full_name = Profile._meta.get_field("full_name")
    letterhead = Profile._meta.get_field("letterhead")
    for target in ("test_app.Profile", Profile):
        with denorm.deferred(target):
            assert _current.get().fields == {full_name, letterhead}
    for target in ("test_app.Profile.full_name", full_name):
        with denorm.deferred(target):
            assert _current.get().fields == {full_name}
    # Model target includes inherited (abstract) denorm fields, but only
    # THIS model's copy of them.
    with denorm.deferred(Forum):
        fields = _current.get().fields
        assert Forum._meta.get_field("tags_string") in fields
        assert Forum._meta.get_field("authors") not in fields  # m2m


# --- default path unchanged --------------------------------------------------


def test_no_block_computes_synchronously(transactional_db, denorm_triggers):
    from test_app.models import Profile

    p = _settled_profile()
    p.first_name = "Jane"
    with _spy(Profile, "full_name") as spy:
        p.save()
    assert spy.call_count == 1
    p.refresh_from_db()
    assert p.full_name == "Jane Doe"


# --- UPDATE / INSERT ---------------------------------------------------------


def test_update_is_deferred_and_marked(transactional_db, denorm_triggers):
    from test_app.models import Profile

    from denorm import denorms

    p = _settled_profile()
    with denorm.deferred("test_app.Profile.full_name"):
        p.first_name = "Jane"
        with _spy(Profile, "full_name") as spy:
            p.save()
        assert spy.call_count == 0, "deferred field must not be computed"
        # Column left as it was (UPDATE wrote full_name = full_name).
        assert Profile.objects.get(pk=p.pk).full_name == "John Doe"

    # full_name is a "safe" self-func: _drop_redundant_self_markers deleted the
    # trigger's marker in post_save; the exit-time marker must survive it.
    assert "full_name" in _markers(Profile, p.pk)

    denorms.flush()
    p.refresh_from_db()
    assert p.full_name == "Jane Doe"
    assert p.letterhead == "Dear Jane Doe"
    assert not _markers(Profile, p.pk)


def test_insert_uses_default_and_is_marked(transactional_db, denorm_triggers):
    """INSERT stores the field default; on Django 6 pre_save runs twice per
    INSERT, so this also covers idempotency."""
    from test_app.models import Profile

    from denorm import denorms

    with denorm.deferred(Profile):
        with _spy(Profile, "full_name") as spy:
            p = Profile.objects.create(first_name="Ann", last_name="Lee")
        assert spy.call_count == 0
        row = Profile.objects.get(pk=p.pk)
        assert row.full_name == ""  # CharField default
        assert row.letterhead == ""

    assert {"full_name", "letterhead"} <= _markers(Profile, p.pk)
    denorms.flush()
    p.refresh_from_db()
    assert (p.full_name, p.letterhead) == ("Ann Lee", "Dear Ann Lee")


def test_insert_not_null_without_default_computes(transactional_db, denorm_triggers):
    """NOT NULL + no default: deferring would only fail the INSERT, so the
    value is computed normally."""
    from test_app.models import CallCounter

    with denorm.deferred(CallCounter):
        c = CallCounter.objects.create()
    c.refresh_from_db()
    assert c.called_count == 1


def test_update_then_insert_fallback(transactional_db, denorm_triggers):
    """An explicit pk makes Django try UPDATE (pre_save add=False -> F()),
    then INSERT (pre_save add=True). The F() must not leak into the INSERT."""
    from test_app.models import Profile

    from denorm import denorms

    with denorm.deferred(Profile):
        p = Profile(pk=987654, first_name="Max", last_name="Payne")
        p.save()
    assert Profile.objects.get(pk=987654).full_name == ""
    assert "full_name" in _markers(Profile, 987654)
    denorms.flush()
    p.refresh_from_db()
    assert p.full_name == "Max Payne"


def test_update_fields(transactional_db, denorm_triggers):
    from test_app.models import Profile

    p = _settled_profile()
    with denorm.deferred(Profile), _spy(Profile, "full_name") as spy:
        p.nickname = "JD"
        p.save(update_fields=["nickname"])  # field not touched at all
    assert spy.call_count == 0
    assert "full_name" not in _markers(Profile, p.pk)

    with denorm.deferred(Profile), _spy(Profile, "full_name") as spy:
        p.first_name = "Jim"
        p.save(update_fields=["first_name", "full_name"])
    assert spy.call_count == 0
    assert "full_name" in _markers(Profile, p.pk)
    assert Profile.objects.get(pk=p.pk).full_name == "John Doe"


def test_other_fields_and_models_still_compute(transactional_db, denorm_triggers):
    from test_app.models import Forum, Post

    forum = Forum.objects.create(title="F")
    # Only Forum.tags_string is deferred; Post's own copy of the inherited
    # field and Post.forum_title compute as usual.
    with denorm.deferred("test_app.Forum.tags_string"):
        with _spy(Post, "forum_title") as spy:
            post = Post.objects.create(forum=forum, title="p")
        assert spy.call_count >= 1
    assert Post.objects.get(pk=post.pk).forum_title == "F"


# --- nesting, transactions, errors -------------------------------------------


def test_nested_blocks_write_markers_at_outermost_exit(
    transactional_db, denorm_triggers
):
    from test_app.models import Forum, Post, Profile

    from denorm.models import DirtyInstance

    p = _settled_profile()
    forum = Forum.objects.create(title="F")
    post = Post.objects.create(forum=forum, title="p")
    DirtyInstance.objects.all().delete()

    with denorm.deferred(Profile):
        with denorm.deferred("test_app.Post.forum_title"):
            p.first_name = "Inner"
            p.save()
            with _spy(Post, "forum_title") as spy:
                post.save()
            assert spy.call_count == 0  # union of targets
        assert "full_name" not in _markers(Profile, p.pk)  # not yet
        with _spy(Post, "forum_title") as spy:
            post.save()  # inner target gone after inner exit
        assert spy.call_count == 1
    assert "full_name" in _markers(Profile, p.pk)
    assert "forum_title" in _markers(Post, post.pk)


def test_rolled_back_savepoint_inside_block(transactional_db, denorm_triggers):
    from test_app.models import Profile

    from denorm import denorms

    p = _settled_profile()
    with transaction.atomic(), denorm.deferred(Profile):
        try:
            with transaction.atomic():
                p.first_name = "Ghost"
                p.save()
                gone = Profile.objects.create(first_name="X", last_name="Y")
                raise RuntimeError("roll back the savepoint")
        except RuntimeError:
            pass
    # Markers for a rolled-back UPDATE / INSERT are harmless: flush recomputes
    # the unchanged row and cleans up the marker of the missing one.
    assert not Profile.objects.filter(pk=gone.pk).exists()
    denorms.flush()
    assert not _markers(Profile, p.pk) and not _markers(Profile, gone.pk)
    assert Profile.objects.get(pk=p.pk).full_name == "John Doe"


def test_exception_in_atomic_rolls_everything_back(transactional_db, denorm_triggers):
    from test_app.models import Profile

    from denorm.models import DirtyInstance

    p = _settled_profile()
    with pytest.raises(RuntimeError):
        with transaction.atomic(), denorm.deferred(Profile):
            p.first_name = "Nope"
            p.save()
            raise RuntimeError("boom")
    assert not DirtyInstance.objects.exists()
    assert Profile.objects.get(pk=p.pk).first_name == "John"
    assert not deferral_active()


def test_exception_in_autocommit_still_marks_saved_rows(
    transactional_db, denorm_triggers
):
    """Outside a transaction the save is already committed when the block
    raises; the marker must still be written (best-effort)."""
    from test_app.models import Profile

    p = _settled_profile()
    with pytest.raises(RuntimeError):
        with denorm.deferred(Profile):
            p.first_name = "Committed"
            p.save()
            raise RuntimeError("boom")
    assert "full_name" in _markers(Profile, p.pk)


def test_exit_in_autocommit_commits_markers(transactional_db, denorm_triggers):
    from test_app.models import Profile

    p = _settled_profile()
    with denorm.deferred(Profile):
        p.first_name = "Auto"
        p.save()
    assert "full_name" in _markers(Profile, p.pk)


# --- interaction with flush / eager ------------------------------------------


def test_flush_inside_block_computes(transactional_db, denorm_triggers):
    from test_app.models import Profile

    from denorm import denorms

    p = _settled_profile()
    with denorm.deferred(Profile):
        p.first_name = "Flushy"
        p.save()
        denorm.mark_dirty(p)
        denorms.flush()  # flush_in_progress(): computes despite the block
        assert Profile.objects.get(pk=p.pk).full_name == "Flushy Doe"


@override_settings(DENORM_ALWAYS_EAGER=True)
def test_eager_is_suppressed_in_block_and_runs_once_at_exit(
    transactional_db, denorm_triggers
):
    from test_app.models import Forum, Post

    from denorm.models import DirtyInstance

    forum = Forum.objects.create(title="F")
    post = Post.objects.create(forum=forum, title="p")
    assert not DirtyInstance.objects.exists()

    with denorm.deferred(Forum):
        forum.title = "Renamed"
        forum.save()
        Post.objects.create(forum=forum, title="second")
        # No eager flush inside the block: dependents are still stale.
        post.refresh_from_db()
        assert post.forum_title == "F"
        assert DirtyInstance.objects.exists()

    post.refresh_from_db()
    forum.refresh_from_db()
    assert post.forum_title == "Renamed"
    assert forum.author_names == ", "  # two posts, no authors
    assert not DirtyInstance.objects.exists()


# --- context isolation -------------------------------------------------------


def test_other_thread_is_not_deferred():
    seen = []
    with denorm.deferred("test_app.Profile"):
        t = threading.Thread(target=lambda: seen.append(deferral_active()))
        t.start()
        t.join()
        assert deferral_active()
    assert seen == [False]
    assert not deferral_active()


def test_asyncio_tasks_are_isolated():
    async def deferring(started, release):
        with denorm.deferred("test_app.Profile"):
            started.set()
            await release.wait()
            return deferral_active()

    async def observer(started, release):
        await started.wait()
        active = deferral_active()
        release.set()
        return active

    async def main():
        started, release = asyncio.Event(), asyncio.Event()
        return await asyncio.gather(
            deferring(started, release), observer(started, release)
        )

    assert asyncio.run(main()) == [True, False]


def test_usable_as_decorator(transactional_db, denorm_triggers):
    from test_app.models import Profile

    p = _settled_profile()

    @denorm.deferred(Profile)
    def edit():
        assert deferral_active()
        p.first_name = "Deco"
        p.save()

    edit()
    edit()  # re-usable: each call gets a fresh context frame
    assert not deferral_active()
    assert "full_name" in _markers(Profile, p.pk)


def test_bulk_create_in_block_is_marked(transactional_db, denorm_triggers):
    from test_app.models import Profile

    from denorm import denorms

    with denorm.deferred("test_app.Profile.full_name"):
        objs = Profile.objects.bulk_create(
            [
                Profile(first_name="A", last_name="B"),
                Profile(first_name="C", last_name="D"),
            ]
        )
        assert [Profile.objects.get(pk=o.pk).full_name for o in objs] == ["", ""]
    for o in objs:
        assert "full_name" in _markers(Profile, o.pk)
    denorms.flush()
    assert sorted(Profile.objects.values_list("full_name", flat=True)) == ["A B", "C D"]
