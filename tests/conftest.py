"""Pytest fixtures for the deadlock test suite."""

from __future__ import annotations

import pytest


@pytest.fixture
def denorm_triggers(transactional_db):
    """Install denorm triggers for the test, drop them afterwards.

    Requires `transactional_db` (not just `db`) because the triggers are
    process-wide DDL and we want concurrent threads to see them committed.
    """
    from denorm import denorms

    denorms.drop_triggers()
    denorms.install_triggers()
    yield
    denorms.drop_triggers()


@pytest.fixture
def thread_runner():
    """Run a callable in N threads, collecting return values and exceptions.

    Each callable gets its own Django connection (Django auto-creates one per
    thread on first access). We close the connection at thread exit to avoid
    leaking connections back into the test DB pool.
    """
    import threading

    from django.db import connections

    def _run(fn, args_list, timeout=60):
        results: list = [None] * len(args_list)
        errors: list = [None] * len(args_list)

        def _wrap(idx, args):
            try:
                results[idx] = fn(*args)
            except BaseException as exc:  # noqa: BLE001 — we want EVERYTHING
                errors[idx] = exc
            finally:
                # Force-close this thread's Django connection. After a
                # failed transaction.atomic() the connection may be in
                # "needs rollback" state; close() will roll it back and
                # release the underlying psycopg2 socket so the test DB
                # can be dropped at teardown.
                for alias in connections:
                    try:
                        connections[alias].close()
                    except Exception:
                        pass

        threads = [
            threading.Thread(target=_wrap, args=(i, args), daemon=True)
            for i, args in enumerate(args_list)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=timeout)
            if t.is_alive():
                raise TimeoutError(
                    f"thread {t.name} did not finish within {timeout}s — likely a hung lock"
                )
        return results, errors

    return _run


@pytest.fixture(scope="session", autouse=True)
def celery_redis():
    """Real Redis for the celery queue path (broker + result + singleton
    lock backend). Eager mode runs tasks inline; the broker is still needed
    because celery-singleton acquires a Redis lock on every apply_async.

    Uses an externally-provided ``DENORM_TEST_REDIS_URL`` (e.g. a CI service
    container) when set; otherwise spins a Redis testcontainer for local dev."""
    import contextlib
    import os

    @contextlib.contextmanager
    def _redis_url():
        existing = os.environ.get("DENORM_TEST_REDIS_URL")
        if existing:
            yield existing
            return
        from testcontainers.redis import RedisContainer

        with RedisContainer("redis:7-alpine") as rc:
            yield (
                f"redis://{rc.get_container_host_ip()}:"
                f"{rc.get_exposed_port(6379)}/0"
            )

    with _redis_url() as url:
        os.environ["DENORM_TEST_REDIS_URL"] = url

        # The celery app is configured via config_from_object("django.conf:
        # settings"), which reads CELERY_* keys lazily from Django settings on
        # every access. Those settings were evaluated at import time (before
        # this fixture ran), so they hold the "memory://" defaults. Override
        # the Django settings attributes so the live, lazily-read app.conf
        # picks up the real Redis URL.
        from django.conf import settings as dj_settings

        dj_settings.CELERY_BROKER_URL = url
        dj_settings.CELERY_RESULT_BACKEND = url

        # Also poke the live app.conf and drop any cached singleton backend on
        # our tasks, covering the case where they were resolved earlier.
        from test_denorm_project.celery_app import app

        app.conf.broker_url = url
        app.conf.result_backend = url
        app.conf.task_always_eager = True
        app.conf.task_eager_propagates = True

        from denorm import tasks

        for t in (tasks.flush_single, tasks.flush_batch, tasks.flush_via_queue):
            t._singleton_backend = None
            t._singleton_config = None

        yield url


@pytest.fixture
def live_worker(celery_redis):
    """A real in-process celery worker with eager OFF, for end-to-end
    queue tests (genuine broker round-trip). Function-scoped: flips eager
    off for its duration and restores it after, so other tests keep the
    fast eager path."""
    from celery.contrib.testing.worker import start_worker
    from django.conf import settings as dj_settings

    from test_denorm_project.celery_app import app

    # CELERY_TASK_ALWAYS_EAGER is sourced lazily from Django settings via
    # config_from_object — the same shadowing that hid broker_url. Flipping
    # only app.conf is overridden on the next lazy read, so tasks would still
    # run inline (EagerResult). Flip the Django setting too, and restore both.
    prev_conf = app.conf.task_always_eager
    prev_dj = dj_settings.CELERY_TASK_ALWAYS_EAGER
    dj_settings.CELERY_TASK_ALWAYS_EAGER = False
    app.conf.task_always_eager = False
    try:
        with start_worker(
            app, pool="solo", perform_ping_check=False, loglevel="error"
        ):
            yield
    finally:
        app.conf.task_always_eager = prev_conf
        dj_settings.CELERY_TASK_ALWAYS_EAGER = prev_dj
