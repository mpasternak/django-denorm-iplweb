"""Opt-in deferral of ``@denormalized`` fields: ``denorm.deferred()``.

By default a ``@denormalized`` field is recomputed synchronously in
``Field.pre_save()`` every time its model is saved. That is the right default,
but it can be wasteful or harmful when the value is about to be invalidated
anyway, e.g. a Django admin change form saves the parent object *before* its
inlines, so a field computed from the inline rows is computed on stale data
and then marked dirty again by the inline triggers. Worse, the computation may
read/lock other tables before the parent row's UPDATE, which can produce lock
ordering (ABBA) deadlocks against a concurrent ``flush_single``.

Inside a ``with denorm.deferred(...)`` block the targeted fields are NOT
computed on save:

* on UPDATE the field's ``pre_save()`` returns ``F(<column>)`` -- the stored
  value is left untouched (same trick as ``AggregateField``);
* on INSERT it returns the field's default (``get_default()``), unless the
  column is NOT NULL and has no usable default, in which case the value is
  computed normally (deferring would only raise an ``IntegrityError``);
* every deferred (instance, field) pair is collected, and when the OUTERMOST
  block exits normally, one ``DirtyInstance`` marker per pair is
  bulk-inserted (``ignore_conflicts=True``) on the instance's database, in the
  caller's current transaction. A regular ``flush()`` / the queue then
  computes the field once, on committed data.

Nothing changes outside a block (the cost is a single ``ContextVar.get()``).

The state lives in a :class:`contextvars.ContextVar`, so it is isolated per
thread and per asyncio task, and it follows code run through
``asgiref.sync.sync_to_async`` (which copies the context).
"""

from __future__ import annotations

import contextvars
import logging
from contextlib import contextmanager

from django.db import models

logger = logging.getLogger(__name__)

# Current deferral frame, or None outside any block. A frame is a
# ``_Frame``; nested blocks push a new frame that unions the targets and
# shares the outermost frame's collector.
_current = contextvars.ContextVar("denorm_deferred", default=None)


class _Frame:
    __slots__ = ("fields", "pending")

    def __init__(self, fields, pending):
        self.fields = fields  # frozenset of field objects (identity match)
        # Shared across nested frames:
        # {(model, pk-or-identity, field_name): (instance, pk-or-None)};
        # pk is None for an INSERT, read from the instance at exit.
        self.pending = pending


def _is_deferrable(field):
    """True for concrete ``@denormalized`` fields (computed in pre_save)."""
    return (
        getattr(field, "_denorm_deferrable", False)
        and field.concrete
        # M2M denorms never reach pre_save; Django < 6.1 reports them as
        # concrete, so exclude them explicitly.
        and not field.many_to_many
    )


def _deferrable_fields_of(model):
    return [f for f in model._meta.concrete_fields if _is_deferrable(f)]


def _resolve_target(target):
    """Turn one user target into a list of field objects (or raise)."""
    from django.apps import apps

    if isinstance(target, type) and issubclass(target, models.Model):
        fields = _deferrable_fields_of(target)
        if not fields:
            raise ValueError(
                f"denorm.deferred(): model {target._meta.label} has no "
                "@denormalized fields that can be deferred."
            )
        return fields

    if isinstance(target, models.Field):
        if not _is_deferrable(target):
            raise ValueError(
                f"denorm.deferred(): {target!r} is not a concrete "
                "@denormalized field."
            )
        return [target]

    if isinstance(target, str):
        parts = target.split(".")
        if len(parts) == 2:
            return _resolve_target(apps.get_model(target))
        if len(parts) == 3:
            model = apps.get_model(parts[0], parts[1])
            field = model._meta.get_field(parts[2])
            if not _is_deferrable(field):
                raise ValueError(
                    f"denorm.deferred(): {target!r} is not a concrete "
                    "@denormalized field (CountField, SumField, CacheKeyField "
                    "and ManyToManyField-based denorms cannot be deferred)."
                )
            return [field]

    raise TypeError(
        "denorm.deferred() targets must be 'app_label.Model', "
        "'app_label.Model.field_name', a model class or a field instance; "
        f"got {target!r}."
    )


def deferral_active():
    """True while the current context is inside a ``deferred()`` block."""
    return _current.get() is not None


def defer_pre_save(field, model_instance, add):
    """Hook called by ``DenormDBField.pre_save``.

    Returns ``(True, value)`` when the field is deferred for this save, else
    ``(False, None)`` and the caller computes the value as usual.
    """
    frame = _current.get()
    if frame is None or field not in frame.fields:
        return False, None

    # flush()'s own recompute save must compute -- that is the deferred work.
    from denorm.denorms import flush_in_progress

    if flush_in_progress():
        return False, None

    if add:
        value = field.get_default()
        if value is None and not field.null:
            # NOT NULL without a usable default: deferring would only make
            # the INSERT fail. Compute normally (the row is brand new, so no
            # concurrent flush can hold a lock on it anyway).
            return False, None
        setattr(model_instance, field.attname, value)
    else:
        value = models.F(field.attname)

    # On INSERT the pk (and _state.db) may not be known yet; both are read
    # from the instance when the block exits. Key by identity until then.
    pk = None if add else model_instance.pk
    ident = pk if pk is not None else ("instance", id(model_instance))
    key = (field.model, ident, field.name)
    frame.pending[key] = (model_instance, pk)
    return True, value


def _write_markers(pending):
    """Insert one DirtyInstance per deferred (db, model, pk, field)."""
    from django.contrib.contenttypes.models import ContentType
    from django.db import transaction

    from denorm.models import DirtyInstance

    by_db = {}
    seen = set()
    for (model, _ident, field_name), (instance, pk) in pending.items():
        if pk is None:
            pk = instance.pk
        if pk is None:
            continue  # the save never completed
        db = instance._state.db or "default"
        key = (db, model, pk, field_name)
        if key in seen:
            continue
        seen.add(key)
        ct = ContentType.objects.db_manager(db).get_for_model(model)
        by_db.setdefault(db, []).append(
            DirtyInstance(content_type=ct, object_id=pk, func_name=field_name)
        )

    for db, markers in by_db.items():
        conn = transaction.get_connection(db)
        if conn.in_atomic_block and conn.needs_rollback:
            # The surrounding transaction is doomed; its rows (and any
            # markers) roll back anyway.
            continue
        DirtyInstance.objects.using(db).bulk_create(markers, ignore_conflicts=True)


def _write_markers_after_error(pending):
    """Best-effort marker write when the block exits with an exception.

    The caller may catch the exception and still commit the rows saved
    before it, so the markers are still wanted. They are written in a
    savepoint, and a failure is logged rather than raised: it must never
    mask the exception that is already propagating.
    """
    from django.db import transaction

    conn = transaction.get_connection()
    if conn.in_atomic_block and conn.needs_rollback:
        return  # doomed transaction: everything saved in it rolls back
    try:
        with transaction.atomic(savepoint=True):
            _write_markers(pending)
    except Exception:
        # Typically "current transaction is aborted" after a database error.
        # Logged, never raised: the original exception is already propagating.
        logger.warning(
            "denorm.deferred(): could not write deferred markers after an "
            "exception; if the saved rows are committed anyway, mark them "
            "with denorm.mark_dirty().",
            exc_info=True,
        )


@contextmanager
def deferred(*targets):
    """Defer the computation of the given ``@denormalized`` fields.

    ``targets`` (at least one is required):

    * ``"app_label.Model.field_name"`` -- one field;
    * ``"app_label.Model"`` or a model class -- every concrete
      ``@denormalized`` field of that model (including inherited ones);
    * a field instance (``Model._meta.get_field("name")``).

    Example::

        with transaction.atomic(), denorm.deferred("bpp.Wydawnictwo_Ciagle"):
            obj.save()
            for inline in inlines:
                inline.save()

    Blocks nest (targets are unioned). Markers are written when the outermost
    block exits (on an exception too, best-effort, unless the surrounding
    transaction is already marked for rollback); after a normal exit with
    ``DENORM_ALWAYS_EAGER`` a single ``flush()`` follows. Saved instances keep a stale in-memory value for the
    deferred fields -- call ``refresh_from_db()`` after the flush.
    """
    if not targets:
        raise TypeError(
            "denorm.deferred() requires at least one target: deferring every "
            "denormalized field implicitly would make unrelated fields stale."
        )
    fields = set()
    for target in targets:
        fields.update(_resolve_target(target))

    outer = _current.get()
    if outer is None:
        frame = _Frame(frozenset(fields), {})
    else:
        frame = _Frame(outer.fields | fields, outer.pending)

    token = _current.set(frame)
    try:
        yield
    except BaseException:
        _current.reset(token)
        if outer is None and frame.pending:
            _write_markers_after_error(frame.pending)
        raise
    _current.reset(token)

    if outer is not None:
        return  # the outermost block writes the markers

    if frame.pending:
        _write_markers(frame.pending)

    from denorm import eager

    eager._eager_flush(sender=None)
