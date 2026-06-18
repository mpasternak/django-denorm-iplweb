"""Misc concurrency reproducers: autotime data race, middleware, circular-denorm livelock.

Split out of the former tests/test_deadlocks.py (see that file's history).
Shared fixtures live in tests/conftest.py.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

import pytest
from django.contrib.contenttypes.models import ContentType
from django.db import OperationalError


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
