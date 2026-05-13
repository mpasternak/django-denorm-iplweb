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
