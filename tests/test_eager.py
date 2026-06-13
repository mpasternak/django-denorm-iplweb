"""DENORM_ALWAYS_EAGER: synchronous flush after each write (test-only)."""
from __future__ import annotations

from django.test import override_settings


@override_settings(DENORM_ALWAYS_EAGER=True)
def test_dependent_denorm_correct_without_manual_flush(
    transactional_db, denorm_triggers
):
    """A write that dirties a DEPENDENT object settles immediately, no
    explicit denorm.flush()."""
    from test_app.models import Forum, Post

    from denorm import denorms
    from denorm.models import DirtyInstance

    forum = Forum.objects.create(title="Orig")
    post = Post.objects.create(forum=forum, title="p")
    denorms.flush()  # settle setup
    DirtyInstance.objects.all().delete()

    forum.title = "Renamed"
    forum.save()  # eager should flush -> Post.forum_title recomputed

    post.refresh_from_db()
    assert post.forum_title == "Renamed", "eager did not settle the dependent object"
    assert not DirtyInstance.objects.exists()


def test_off_by_default_leaves_dependent_dirty(transactional_db, denorm_triggers):
    """Without the setting, the dependent stays stale until a manual flush
    (proves opt-in + the handler is a no-op when off)."""
    from test_app.models import Forum, Post

    from denorm import denorms
    from denorm.models import DirtyInstance

    forum = Forum.objects.create(title="Orig")
    post = Post.objects.create(forum=forum, title="p")
    denorms.flush()
    DirtyInstance.objects.all().delete()

    forum.title = "Renamed"
    forum.save()  # NOT eager

    assert DirtyInstance.objects.exists(), "expected a pending marker (deferred)"
    post.refresh_from_db()
    assert post.forum_title == "Orig", "dependent should still be stale before flush"

    denorms.flush()
    post.refresh_from_db()
    assert post.forum_title == "Renamed"


@override_settings(DENORM_ALWAYS_EAGER=True)
def test_same_model_chain_settles_eagerly(transactional_db, denorm_triggers):
    """full_name -> letterhead chain correct after a plain save, no manual
    flush (exercises re-entrancy + convergence under eager)."""
    from test_app.models import Profile

    from denorm.models import DirtyInstance

    p = Profile.objects.create(first_name="John", last_name="Doe")
    # create() itself fires post_save -> eager flush
    p.refresh_from_db()
    assert p.full_name == "John Doe"
    assert p.letterhead == "Dear John Doe"
    assert not DirtyInstance.objects.exists()

    p.last_name = "Smith"
    p.save()
    p.refresh_from_db()
    assert p.full_name == "John Smith"
    assert p.letterhead == "Dear John Smith"
    assert not DirtyInstance.objects.exists()


@override_settings(DENORM_ALWAYS_EAGER=True)
def test_m2m_change_settles_eagerly(transactional_db, denorm_triggers):
    """An m2m relation edit settles the m2m-dependent denorm field without a
    manual flush, proving the m2m_changed signal wiring in eager.connect().

    Model choice: Member.bookmark_titles depends on Member.bookmarks (a
    plain ManyToManyField to Post) via @depend_on_related("Post",
    foreign_key="bookmarks").  Its return value is a plain TextField
    ("\n".join of Post.title values), giving a clean string assertion.
    Forum.authors was not chosen because it is itself a
    @denormalized(ManyToManyField) whose m2m output requires an extra
    reverse-lookup to assert, adding noise.
    """
    from test_app.models import Forum, Member, Post

    from denorm.models import DirtyInstance

    forum = Forum.objects.create(title="F")
    member = Member.objects.create(first_name="Alice", name="Smith")
    post1 = Post.objects.create(forum=forum, title="Hello World")
    post2 = Post.objects.create(forum=forum, title="Second Post")

    # Add first bookmark via m2m — eager should flush immediately.
    member.bookmarks.add(post1)
    member.refresh_from_db()
    assert member.bookmark_titles == "Hello World", (
        "eager did not settle bookmark_titles after m2m add"
    )

    # Add a second bookmark.
    member.bookmarks.add(post2)
    member.refresh_from_db()
    titles = set(member.bookmark_titles.splitlines())
    assert titles == {"Hello World", "Second Post"}, (
        f"eager did not settle after second m2m add; got {member.bookmark_titles!r}"
    )

    # Remove one bookmark — eager should flush again.
    member.bookmarks.remove(post1)
    member.refresh_from_db()
    assert member.bookmark_titles == "Second Post", (
        "eager did not settle bookmark_titles after m2m remove"
    )

    assert not DirtyInstance.objects.exists(), "DirtyInstance table not empty after eager flush"


@override_settings(DENORM_ALWAYS_EAGER=True)
def test_no_unbounded_recursion(transactional_db, denorm_triggers):
    """flush() saves objects which re-fire post_save; the thread-local guard
    must make those re-entrant calls no-ops (one flush per triggering save,
    no hang)."""
    from test_app.models import Profile

    from denorm import denorms
    from denorm.models import DirtyInstance

    calls = []
    orig = denorms.flush

    def counting_flush(*a, **k):
        calls.append(1)
        return orig(*a, **k)

    import unittest.mock as m

    # The eager handler does `from denorm import denorms; denorms.flush()`,
    # so patching the attribute on the denorms module is observed by it.
    with m.patch.object(denorms, "flush", counting_flush):
        # Completion (no hang / RecursionError) is itself proof the
        # thread-local guard short-circuits the re-entrant signals.
        Profile.objects.create(first_name="A", last_name="B")

    assert calls, "eager handler never flushed"
    # The guard prevents re-entrant flushes within one save's handler.
    # (Exact count can be >1 across multiple post_save signals from the save,
    # but must be finite/small — the test completing proves no infinite loop.)
    assert len(calls) < 50, f"suspicious flush count {len(calls)} — recursion?"
    assert not DirtyInstance.objects.exists()
