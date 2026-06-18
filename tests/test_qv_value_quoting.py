"""``_qv`` must SQL-escape the value it wraps, not just add quotes.

Review batch 3, finding #3: ``denorm.dependencies.base._qv`` inlined a value
into trigger SQL as a string literal with ``f"'{value}'"`` and no escaping —
the only thing standing between a marker value and broken/injected trigger DDL.
Its sibling ``denorm.helpers.content_type_select_sql._quote`` already doubles
embedded quotes; ``_qv`` must do the same so the invariant is enforced, not
merely assumed of every caller.
"""

from __future__ import annotations

from denorm.dependencies.base import _qv


def test_qv_wraps_plain_identifier():
    # The common case: a Python function name passes through wrapped in quotes.
    assert _qv("update_total") == "'update_total'"


def test_qv_escapes_embedded_single_quote():
    # A value containing a single quote must be doubled, exactly like
    # helpers._quote, so it cannot terminate the literal and inject SQL.
    assert _qv("O'Brien") == "'O''Brien'"


def test_qv_escapes_injection_attempt():
    # Defense in depth: even a hostile value stays inside one quoted literal.
    assert _qv("x'); DROP TABLE denorm_dirtyinstance; --") == (
        "'x''); DROP TABLE denorm_dirtyinstance; --'"
    )
