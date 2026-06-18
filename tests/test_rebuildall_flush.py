"""``rebuildall`` must not let its ``verbose`` flag influence flush convergence.

Review #2: ``rebuildall`` called ``flush(verbose)``. ``flush``'s first positional
parameter is ``run_once`` (a one-pass switch), not a verbosity flag — so
``rebuildall(verbose=True)`` quietly turned into ``flush(run_once=True)``,
stopping after a single pass and leaving dependency-cascade DirtyInstance
markers unprocessed (stale denormalized data). ``verbose`` is logging-only and
must never reach ``flush``'s convergence control.

These tests mock ``flush`` and the denorm registry, so they touch no database.
"""

from __future__ import annotations

from unittest.mock import patch


def _call_rebuildall(**kwargs):
    with patch("denorm.denorms.triggerset.get_alldenorms", return_value=[]), patch(
        "denorm.denorms.flush"
    ) as mock_flush:
        from denorm.denorms import rebuildall

        rebuildall(**kwargs)
    return mock_flush


def _run_once_arg(mock_flush):
    args, kwargs = mock_flush.call_args
    return kwargs.get("run_once", args[0] if args else False)


def test_rebuildall_verbose_does_not_force_run_once():
    mock_flush = _call_rebuildall(verbose=True)

    mock_flush.assert_called_once()
    assert not _run_once_arg(mock_flush)


def test_rebuildall_default_does_not_force_run_once():
    mock_flush = _call_rebuildall()

    mock_flush.assert_called_once()
    assert not _run_once_arg(mock_flush)
