"""Behaviour when the database goes away (restart, failover, crash).

* ``is_connection_error`` tells a lost/unavailable connection apart from other
  database errors.
* The queue tasks (flush_via_queue, _flush_requeue, flush_batch) log a WARNING
  and give up quietly on a lost connection instead of failing: the dirty
  markers persist and the denorm_queue daemon re-dispatches on reconnect.
* The denorm_queue daemon only WARNs during DENORM_QUEUE_OUTAGE_GRACE, then
  logs an ERROR at most every DENORM_QUEUE_OUTAGE_REPEAT seconds, and resets
  once the connection is restored.

Everything is mocked: no real database outage is needed.
"""

from __future__ import annotations

import logging
import time as time_mod

import psycopg2
import pytest
from django.db import InterfaceError, OperationalError

from denorm import denorms, tasks
from denorm.conf import settings as denorm_settings
from denorm.management.commands import denorm_queue as queue_mod
from denorm.retry import is_connection_error

SERVER_CLOSED = (
    "server closed the connection unexpectedly\n"
    "\tThis probably means the server terminated abnormally\n"
    "\tbefore or while processing the request.\n"
)


def _with_pgcode(exc_class, message, pgcode):
    exc = exc_class(message)
    # psycopg2 exposes pgcode as a read-only attribute; a subclass lets the
    # test set it the way a real server error would carry it.
    exc.__cause__ = type("FakePgError", (psycopg2.OperationalError,), {"pgcode": pgcode})(message)
    return exc


def _mock_connection():
    from unittest.mock import MagicMock

    conn_wrapper = MagicMock()
    conn_wrapper.connection.notifies = []
    return conn_wrapper


# --- is_connection_error ----------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        OperationalError(SERVER_CLOSED),
        OperationalError(
            'connection to server at "dbserver" (172.18.0.17), port 5432 failed: '
            "FATAL:  the database system is shutting down"
        ),
        OperationalError("could not connect to server: Connection refused"),
        psycopg2.OperationalError(SERVER_CLOSED),
        InterfaceError("connection already closed"),
        psycopg2.InterfaceError("connection already closed"),
        _with_pgcode(OperationalError, "terminating connection due to administrator command", "57P01"),
        _with_pgcode(OperationalError, "connection failure", "08006"),
    ],
)
def test_is_connection_error_true(exc):
    assert is_connection_error(exc)


@pytest.mark.parametrize(
    "exc",
    [
        OperationalError("deadlock detected"),
        _with_pgcode(OperationalError, "deadlock detected", "40P01"),
        _with_pgcode(OperationalError, "could not serialize access", "40001"),
        OperationalError('relation "foo" does not exist'),
        ValueError(SERVER_CLOSED),
    ],
)
def test_is_connection_error_false(exc):
    assert not is_connection_error(exc)


# --- queue tasks -------------------------------------------------------------


def _raise(exc):
    def _f(*args, **kwargs):
        raise exc

    return _f


def test_flush_via_queue_gives_up_quietly_on_lost_connection(monkeypatch, caplog):
    monkeypatch.setattr(tasks, "_chunk_distinct_pairs", _raise(OperationalError(SERVER_CLOSED)))

    with caplog.at_level(logging.WARNING, logger="denorm.tasks"):
        assert tasks.flush_via_queue() is None

    assert any("database connection lost" in r.getMessage() for r in caplog.records)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_flush_via_queue_still_raises_other_database_errors(monkeypatch):
    monkeypatch.setattr(
        tasks, "_chunk_distinct_pairs", _raise(OperationalError('relation "x" does not exist'))
    )

    with pytest.raises(OperationalError):
        tasks.flush_via_queue()


def test_flush_requeue_gives_up_quietly_on_lost_connection(monkeypatch):
    from denorm.models import DirtyInstance

    monkeypatch.setattr(DirtyInstance.objects, "exists", _raise(OperationalError(SERVER_CLOSED)))
    dispatched = []
    monkeypatch.setattr(tasks.flush_via_queue, "delay", lambda **kw: dispatched.append(kw))

    assert tasks._flush_requeue([], next_pass=1) is None
    assert dispatched == []


def test_flush_batch_aborts_on_lost_connection(monkeypatch, caplog):
    calls = []

    def fake_flush_single(content_type_id, object_id):
        calls.append(object_id)
        if object_id == 2:
            raise OperationalError(SERVER_CLOSED)

    monkeypatch.setattr(denorms, "flush_single", fake_flush_single)

    with caplog.at_level(logging.WARNING, logger="denorm.tasks"):
        result = tasks.flush_batch([(1, 1), (1, 2), (1, 3), (1, 4)])

    # Stops at the first lost connection instead of failing every other pair.
    assert calls == [1, 2]
    assert result == {"processed": 1, "errors": 0, "aborted": True}
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_flush_batch_skips_pair_on_other_database_error(monkeypatch):
    calls = []

    def fake_flush_single(content_type_id, object_id):
        calls.append(object_id)
        if object_id == 2:
            raise OperationalError('relation "x" does not exist')

    monkeypatch.setattr(denorms, "flush_single", fake_flush_single)

    result = tasks.flush_batch([(1, 1), (1, 2), (1, 3)])

    # Per-pair isolation is unchanged for errors other than a lost connection.
    assert calls == [1, 2, 3]
    assert result == {"processed": 3, "errors": 1}


# --- denorm_queue daemon -----------------------------------------------------


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(time_mod, "monotonic", clock.monotonic)
    monkeypatch.setattr(time_mod, "sleep", clock.sleep)
    return clock


@pytest.fixture
def outage_settings(monkeypatch):
    monkeypatch.setattr(denorm_settings, "DENORM_QUEUE_OUTAGE_GRACE", 15 * 60)
    monkeypatch.setattr(denorm_settings, "DENORM_QUEUE_OUTAGE_REPEAT", 30 * 60)


def _error_times(caplog, start):
    return [
        round(r.created_monotonic - start)
        for r in caplog.records
        if r.levelno >= logging.ERROR and r.name == queue_mod.__name__
    ]


@pytest.fixture
def stamp_records(monkeypatch, clock):
    """Stamp each log record with the fake clock, so tests can tell WHEN an
    ERROR was logged."""
    original = logging.LogRecord.__init__

    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.created_monotonic = clock.now

    monkeypatch.setattr(logging.LogRecord, "__init__", init)


def test_daemon_warns_during_grace_then_errors_every_repeat(
    monkeypatch, caplog, clock, outage_settings, stamp_records
):
    cmd = queue_mod.Command()
    start = clock.now
    fail_until = start + 80 * 60  # an 80-minute outage: ERRORs at ~15, ~45 and ~75 min

    def fake_loop(run_once=False):
        if clock.now < fail_until:
            raise psycopg2.OperationalError(SERVER_CLOSED)
        # Connected: simulate the reset _listen_loop performs, then exit.
        cmd._backoff = cmd.base_backoff

    monkeypatch.setattr(cmd, "_listen_loop", fake_loop)

    with caplog.at_level(logging.WARNING, logger=queue_mod.__name__):
        cmd.handle(run_once=False)

    errors = _error_times(caplog, start)
    # First ERROR once 15 min have passed (first attempt at or after 900 s; the
    # backoff is capped at 30 s), then one per 30 min, never more often.
    assert len(errors) == 3
    assert 900 <= errors[0] < 900 + cmd.max_backoff
    assert all(b - a >= 30 * 60 for a, b in zip(errors, errors[1:]))
    # Constant message, so an error tracker groups the whole outage together.
    messages = {r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR}
    assert len(messages) == 1
    # Before the grace period there were only warnings.
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_daemon_short_outage_logs_no_error(monkeypatch, caplog, clock, outage_settings):
    cmd = queue_mod.Command()
    fail_until = clock.now + 2 * 60  # a typical restart

    def fake_loop(run_once=False):
        if clock.now < fail_until:
            raise psycopg2.OperationalError(SERVER_CLOSED)

    monkeypatch.setattr(cmd, "_listen_loop", fake_loop)

    with caplog.at_level(logging.WARNING, logger=queue_mod.__name__):
        cmd.handle(run_once=False)

    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_daemon_reconnect_resets_outage_clock(monkeypatch, caplog, clock, outage_settings):
    # Drive _listen_loop for real (mocked connection), so the reset it performs
    # on a successful LISTEN is what is being tested.
    conn_wrapper = _mock_connection()
    monkeypatch.setattr(queue_mod, "connection", conn_wrapper)
    monkeypatch.setattr(queue_mod.flush_via_queue, "delay", lambda: None)
    monkeypatch.setattr(queue_mod.select, "select", lambda r, w, x, t: ([], [], []))

    cmd = queue_mod.Command()
    cmd._outage_started = clock.now - 20 * 60
    cmd._last_outage_error = clock.now - 5 * 60

    with caplog.at_level(logging.WARNING, logger=queue_mod.__name__):
        cmd._listen_loop(run_once=True)

    assert cmd._outage_started is None
    assert cmd._last_outage_error is None
    assert any("connection restored after 1200s" in r.getMessage() for r in caplog.records)

    # A new drop right after the reconnect starts a fresh grace period: a
    # WARNING, not an immediate ERROR carried over from the previous outage.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=queue_mod.__name__):
        cmd._report_connection_loss(psycopg2.OperationalError(SERVER_CLOSED))
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
