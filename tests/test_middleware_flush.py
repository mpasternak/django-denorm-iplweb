"""DenormMiddleware flush behaviour: exists() guard + inline/queue/off modes.

Review (middleware): ``process_response`` called ``flush()`` unconditionally on
every response — even when nothing was dirty (wasteful) and even when a queue
is the intended flush path. It is now gated on ``DirtyInstance.objects.exists()``
(a cheap LIMIT-1 guard that is correct for bulk/raw writes too, since markers
come from DB triggers) and on the ``DENORM_MIDDLEWARE_FLUSH`` mode setting:

* ``"inline"`` (default) — flush synchronously in the response cycle.
* ``"queue"``  — dispatch ``flush_via_queue`` to Celery, don't block the request.
* ``"off"``    — never flush; rely on the ``denorm_queue`` daemon or manual flush.

The setting is read LIVE (like DENORM_ALWAYS_EAGER) so override_settings works.
These tests mock the flush sinks and the marker existence check — no database.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import override_settings

from denorm.middleware import DenormMiddleware


def _run(exists):
    mw = DenormMiddleware(get_response=lambda r: r)
    response = object()
    with patch("denorm.middleware.flush") as mock_flush, patch(
        "denorm.middleware.DirtyInstance"
    ) as mock_di, patch("denorm.tasks.flush_via_queue") as mock_task:
        mock_di.objects.exists.return_value = exists
        out = mw.process_response(object(), response)
    assert out is response
    return mock_flush, mock_task


@override_settings(DENORM_MIDDLEWARE_FLUSH="inline")
def test_inline_flushes_when_dirty():
    mock_flush, mock_task = _run(exists=True)
    mock_flush.assert_called_once_with()
    mock_task.delay.assert_not_called()


@override_settings(DENORM_MIDDLEWARE_FLUSH="inline")
def test_inline_skips_when_clean():
    mock_flush, mock_task = _run(exists=False)
    mock_flush.assert_not_called()


@override_settings(DENORM_MIDDLEWARE_FLUSH="off")
def test_off_never_flushes_even_when_dirty():
    mock_flush, mock_task = _run(exists=True)
    mock_flush.assert_not_called()
    mock_task.delay.assert_not_called()


@override_settings(DENORM_MIDDLEWARE_FLUSH="queue")
def test_queue_dispatches_when_dirty():
    mock_flush, mock_task = _run(exists=True)
    mock_flush.assert_not_called()
    mock_task.delay.assert_called_once_with()


@override_settings(DENORM_MIDDLEWARE_FLUSH="queue")
def test_queue_skips_when_clean():
    mock_flush, mock_task = _run(exists=False)
    mock_task.delay.assert_not_called()


def test_default_mode_is_inline():
    # No DENORM_MIDDLEWARE_FLUSH set -> default to current behaviour (inline).
    mock_flush, mock_task = _run(exists=True)
    mock_flush.assert_called_once_with()
