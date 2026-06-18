"""Robustness of the denorm_queue LISTEN/NOTIFY daemon (review #6).

Three fixes, all exercised here with mocks (no real LISTEN/NOTIFY):

1. Reconnect backoff must RESET after a successful (re)connection, so a flapping
   DB does not accumulate an ever-growing wait. handle() must therefore re-read
   the live backoff that _listen_loop resets on connect, not a frozen local.
2. select() must use a FINITE timeout (keepalive) so the daemon can notice a
   silently dead connection and shut down promptly, and a signal-interrupted
   select (InterruptedError) must not crash the daemon.
3. The LISTEN cursor must be closed (used as a context manager).
"""

from __future__ import annotations

import time as time_mod
from unittest.mock import MagicMock

import psycopg2

from denorm.management.commands import denorm_queue as mod


def test_handle_resets_backoff_after_successful_connection(monkeypatch):
    cmd = mod.Command()
    sleeps = []
    monkeypatch.setattr(time_mod, "sleep", lambda s: sleeps.append(s))

    calls = {"n": 0}

    def fake_loop(run_once=False):
        calls["n"] += 1
        # Simulate a successful (re)connect — which resets backoff — followed by
        # a connection drop, twice; then a clean exit.
        cmd._backoff = 1.0
        if calls["n"] <= 2:
            raise psycopg2.OperationalError("connection dropped")

    monkeypatch.setattr(cmd, "_listen_loop", fake_loop)

    cmd.handle(run_once=False)

    # Each drop followed a fresh connect, so both waits are the base 1.0s. The
    # buggy version froze backoff in a local and never reset, making the second
    # wait 2.0s.
    assert sleeps == [1.0, 1.0]


def test_handle_escalates_backoff_while_disconnected(monkeypatch):
    cmd = mod.Command()
    sleeps = []
    monkeypatch.setattr(time_mod, "sleep", lambda s: sleeps.append(s))

    calls = {"n": 0}

    def fake_loop(run_once=False):
        # Never reaches the reset point (connection never re-established).
        calls["n"] += 1
        if calls["n"] <= 3:
            raise psycopg2.OperationalError("still down")

    monkeypatch.setattr(cmd, "_listen_loop", fake_loop)

    cmd.handle(run_once=False)

    assert sleeps == [1.0, 2.0, 4.0]


def _wire_fake_connection(monkeypatch):
    """Patch module-level connection/flush so _listen_loop can run unitarily."""
    conn_wrapper = MagicMock()
    pg_con = MagicMock()
    conn_wrapper.connection = pg_con
    monkeypatch.setattr(mod, "connection", conn_wrapper)
    monkeypatch.setattr(mod.flush_via_queue, "delay", lambda: None)
    return conn_wrapper, pg_con


def test_listen_loop_uses_finite_timeout_and_closes_cursor(monkeypatch):
    conn_wrapper, pg_con = _wire_fake_connection(monkeypatch)

    select_timeouts = []

    def fake_select(r, w, x, timeout):
        select_timeouts.append(timeout)
        return ([], [], [])  # keepalive timeout

    monkeypatch.setattr(mod.select, "select", fake_select)

    cmd = mod.Command()
    cmd._backoff = 9.0
    cmd._listen_loop(run_once=True)

    # Cursor used as a context manager -> closed on exit.
    conn_wrapper.cursor.return_value.__exit__.assert_called()
    # Finite timeout, never the original blocking None.
    assert select_timeouts and select_timeouts[0] is not None
    # Successful connect cleared the backoff.
    assert cmd._backoff == 1.0


def test_listen_loop_survives_interrupted_select(monkeypatch):
    _wire_fake_connection(monkeypatch)

    def fake_select(r, w, x, timeout):
        raise InterruptedError("signal during select")

    monkeypatch.setattr(mod.select, "select", fake_select)

    cmd = mod.Command()
    # Must not propagate InterruptedError.
    cmd._listen_loop(run_once=True)
