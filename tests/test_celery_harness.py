"""Guards the celery test harness fixtures themselves."""
from __future__ import annotations

import threading

from test_denorm_project.celery_app import app


# Registered at MODULE IMPORT time, before live_worker starts its worker.
# A task registered at test time is not in the worker's strategy table and
# gets discarded as "unregistered", so the probe must live at module level.
@app.task(name="tests.harness.thread_probe")
def thread_probe():
    """Return the OS thread id this task body actually executed in."""
    return threading.get_ident()


def test_live_worker_actually_disables_eager(transactional_db, live_worker):
    """live_worker must run tasks in a real worker thread, not inline.
    CELERY_TASK_ALWAYS_EAGER is sourced lazily from Django settings, so
    flipping only app.conf is shadowed — the fixture must flip the Django
    setting too. Discriminator: eager -> EagerResult in the test thread;
    non-eager -> AsyncResult run in the worker thread."""
    result = thread_probe.delay()
    assert type(result).__name__ != "EagerResult", (
        "live_worker ran the task eagerly (inline) — eager was not disabled"
    )
    ran_in = result.get(timeout=30)
    assert ran_in != threading.get_ident(), (
        "task ran in the test thread — eager, not a real worker"
    )
