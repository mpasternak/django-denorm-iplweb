"""DENORM_ALWAYS_EAGER — synchronous flush after each write (test-only).

Mirrors Celery's ``task_always_eager``: when the setting is on, denorm
flushes synchronously after every signalled write so denorm fields — including
those on *dependent* objects and same-model chains — are correct immediately,
with no manual ``denorm.flush()`` and no Celery worker.

The handler is **always connected** in ``AppConfig.ready()`` and reads the
setting **live** (at call time) from ``django.conf.settings`` so that
``@override_settings(DENORM_ALWAYS_EAGER=True)`` toggles it within a test —
gating at connect time would make ``override_settings`` (which cannot re-run
``ready()``) unable to enable it.

Test-only by design: the deferred model (DB triggers mark dirty, ``flush()``
recomputes later) is unchanged in production. Default is OFF, in which case the
handler is a strict no-op (one ``getattr`` per signal).
"""
from __future__ import annotations

import threading

from django.conf import settings
from django.db.models.signals import m2m_changed, post_delete, post_save

# Thread-local (not a plain global) so parallel test workers / threads do not
# disable each other's eager flush.
_state = threading.local()


def _eager_flush(sender, **kwargs):
    # Read the setting LIVE so override_settings(DENORM_ALWAYS_EAGER=True)
    # works. When off, this is a strict no-op.
    if not getattr(settings, "DENORM_ALWAYS_EAGER", False):
        return
    # Re-entrancy guard: flush() saves recomputed objects, whose post_save
    # signals re-enter this handler. The outer flush() already drains
    # everything to convergence, so the re-entrant calls must be no-ops.
    # This also guards DirtyInstance's own post_save during flush().
    if getattr(_state, "flushing", False):
        return
    _state.flushing = True
    try:
        # Lazy import: `from denorm import denorms` is safe at call time
        # (the app registry is fully initialised before any signal fires),
        # but a module-level import would create a circular dependency at
        # app-load time (eager.py is imported by apps.py before the denorm
        # package's __init__ finishes registering all symbols). Keeping the
        # import here avoids that risk with negligible per-call overhead —
        # Python caches it in sys.modules after the first resolution.
        from denorm import denorms

        denorms.flush()
    finally:
        _state.flushing = False


def connect():
    """Wire the eager handler to write signals for all senders (idempotent)."""
    post_save.connect(_eager_flush, dispatch_uid="denorm_eager")
    post_delete.connect(_eager_flush, dispatch_uid="denorm_eager")
    m2m_changed.connect(_eager_flush, dispatch_uid="denorm_eager")
