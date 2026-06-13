from django.db.models.signals import post_save


def _mark_instance_dirty(sender, instance, **kwargs):
    from denorm import denorms

    # flush() recomputes by calling obj.save(), which fires post_save again.
    # Re-marking here would mean flush could never converge for an always-dirty
    # model, so suppress the mark while a flush is recomputing this thread's
    # object. User-initiated saves (outside flush) still mark normally.
    if denorms.flush_in_progress():
        return
    denorms.mark_dirty(instance)


def denorm_always_dirty(model):
    """Class decorator: every save() of ``model`` marks the whole object dirty
    (a ``func_name=NULL`` marker), forcing all its denormalized fields to be
    recomputed on the next :func:`~denorm.flush`.

    Use this when a denormalized field's value depends on inputs that the
    trigger / ``@depend_on_related`` / ``@depend_on_fields`` system cannot
    express — external state, time-based values, complex cross-table reads, etc.
    The decorator guarantees a recompute on every user ``save()``.

    :func:`~denorm.flush` converges normally: it claims the NULL marker,
    recomputes the fields, and clears the marker.  flush's own recompute
    ``save()`` does NOT re-mark the object (a thread-local flush-in-progress
    guard suppresses the handler during flush), so the object ends up clean —
    no perpetual re-marking.  A subsequent user ``save()`` marks it dirty again.

    .. note::

       **post_save only** — does NOT fire for ``QuerySet.update()`` /
       ``bulk_create()`` / raw SQL.  For those paths, call
       :func:`~denorm.mark_dirty` explicitly or run
       ``manage.py denorm_rebuild`` afterwards.

    Example::

        from denorm import denorm_always_dirty, denormalized, depend_on_fields

        @denorm_always_dirty
        class Report(models.Model):
            title = models.CharField(max_length=100)
            extra = models.CharField(max_length=100, default="")

            @denormalized(models.CharField, max_length=120)
            @depend_on_fields("title")
            def heading(self):
                return f"Report: {self.title}"
    """
    post_save.connect(
        _mark_instance_dirty,
        sender=model,
        dispatch_uid=f"denorm_always_dirty_{model._meta.label}",
    )
    return model
