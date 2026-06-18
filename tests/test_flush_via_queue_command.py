"""The ``denorm_flush_via_queue`` management command must drain the dirty table.

Review #3: the command treated the return value of ``flush_via_queue`` as a
Celery ``GroupResult`` and called ``len(...)`` / ``.completed_count()`` on it.
But ``flush_via_queue`` returns the chord callback's ``AsyncResult`` (or nothing),
so the command crashed with ``TypeError`` for any non-empty dirty table. The
fix dispatches the task fire-and-forget and polls ``DirtyInstance`` until the
backlog drains (bounded by ``--timeout``).

Runs under the default eager celery config (``celery_redis`` autouse fixture),
where ``flush_via_queue.delay()`` executes the whole chord synchronously, so the
table is already drained by the time the command starts polling.
"""

from __future__ import annotations


def test_command_drains_dirty_table(denorm_triggers):
    from django.core.management import call_command

    from test_app.models import Member

    from denorm.models import DirtyInstance

    Member.objects.create(first_name="Ada", name="Lovelace")
    Member.objects.create(first_name="Alan", name="Turing")
    assert DirtyInstance.objects.exists(), "setup: expected dirty markers"

    # Must not raise (the bug crashed here on len(group_result)).
    call_command("denorm_flush_via_queue", timeout=30)

    assert not DirtyInstance.objects.exists()


def test_command_handles_empty_table(denorm_triggers):
    from django.core.management import call_command

    from denorm.models import DirtyInstance

    DirtyInstance.objects.all().delete()

    # No work to do — must return cleanly, no exception.
    call_command("denorm_flush_via_queue", timeout=30)

    assert not DirtyInstance.objects.exists()
