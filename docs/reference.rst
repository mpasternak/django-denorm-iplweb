=========
Reference
=========


Decorators
==========

.. autofunction:: denorm.denormalized

.. autofunction:: denorm.depend_on_related(othermodel,foreign_key=None,type=None)

Fields
======

.. autoclass:: denorm.CacheKeyField
   :members: __init__,depend_on_related

.. autoclass:: denorm.CountField
   :members: __init__


Functions
=========

.. autofunction:: denorm.flush

Middleware
==========

.. autoclass:: denorm.middleware.DenormMiddleware


Views
=====

``denorm.views.dirty_instances_count``
--------------------------------------

A read-only view that renders the contents of the ``DirtyInstance`` queue
grouped by content type, together with a total. It is the HTTP equivalent of
the ``denorm_show_dirtyinstances_count`` management command and is meant for
monitoring the denormalization backlog.

To enable the view, include the bundled URLConf in your project::

    # urls.py
    from django.urls import include, path

    urlpatterns = [
        ...
        path("denorm/", include("denorm.urls")),
    ]

After that the view is reachable at ``/denorm/dirty-instances/`` and can be
resolved with ``reverse("denorm:dirty_instances_count")``.

The template ``denorm/dirty_instances_count.html`` extends
``admin/base_site.html``; override it in your own templates directory if you
want a different look.

Access control
^^^^^^^^^^^^^^

Access to the view is configured with the
``DENORM_DIRTY_INSTANCES_VIEW_ACCESS`` setting. It accepts one of:

* ``"staff"`` (default) — wraps the view with
  ``django.contrib.admin.views.decorators.staff_member_required``. Only users
  with ``is_staff=True`` can see the page.
* ``"authenticated"`` — wraps the view with
  ``django.contrib.auth.decorators.login_required``. Any logged-in user can
  see the page.
* ``"public"`` — no access decorator is applied. The page becomes reachable
  without authentication. Use with caution: the response contains the names
  of installed models with pending denormalizations, which leaks information
  about your project layout. Only enable this behind a network perimeter you
  trust.

The setting is read once at import time of ``denorm.views``; changing it at
runtime has no effect.


Management commands
===================

**denorm_init**
    .. automodule:: denorm.management.commands.denorm_init

**denorm_drop**
    .. automodule:: denorm.management.commands.denorm_drop

**denorm_rebuild**
    .. automodule:: denorm.management.commands.denorm_rebuild

**denorm_flush**
    .. automodule:: denorm.management.commands.denorm_flush

**denorm_queue**
    .. automodule:: denorm.management.commands.denorm_queue

**denorm_sql**
    .. automodule:: denorm.management.commands.denorm_sql


Same-model dependencies: ``depend_on_fields``
=============================================

.. function:: denorm.depend_on_fields(*field_names)

   Declares that a :func:`denormalized` function reads the given sibling
   columns of its own model (plain columns or other denormalized fields)::

       class Person(models.Model):
           first_name = models.CharField(max_length=50)
           last_name = models.CharField(max_length=50)

           @denormalized(models.CharField, max_length=101)
           @depend_on_fields("first_name", "last_name")
           def full_name(self):
               return f"{self.first_name} {self.last_name}"

           @denormalized(models.CharField, max_length=120)
           @depend_on_fields("full_name")
           def letterhead(self):
               return f"Dear {self.full_name}"

   A declared function is marked dirty only when a declared column
   changes. An undeclared function keeps conservative semantics: any
   watched column change (except the function's own column) marks it
   dirty. ``@depend_on_fields()`` with no arguments means "reads no
   sibling columns".

   Bulk-writing a denormalized column directly is unsupported — it does
   not mark anything dirty. Write the source columns instead, or use
   :func:`mark_dirty`.

   Declarations are audited by Django system checks (``denorm.E001``,
   ``denorm.E002``, ``denorm.W001``, ``denorm.W002``); an incomplete
   declaration means silent staleness, exactly like a missing
   :func:`depend_on_related`. Silence individual checks via
   ``SILENCED_SYSTEM_CHECKS``.

.. function:: denorm.mark_dirty(*instances)

   Explicitly marks whole objects dirty (``func_name=NULL`` markers).
   NULL means "recompute every denormalized field of this object" and
   takes precedence over field-level markers during flush. The library's
   own triggers never emit NULL; only ``mark_dirty`` and
   ``rebuildall``/``rebuild_instances_of`` do.


Settings
========

``DENORM_MAX_FLUSH_PASSES`` (default ``100``): ``flush()`` aborts with an
error log naming the still-dirty models instead of looping forever when a
denormalized function is non-deterministic.

``DENORM_DIRTY_INSTANCES_VIEW_ACCESS`` (default ``"staff"``): access policy
for the ``dirty_instances_count`` view. Accepts ``"staff"``,
``"authenticated"``, or ``"public"`` — see the Views section above for details.

``DENORM_DISABLE_AUTOTIME_DURING_FLUSH``: when set, disables ``auto_now``
and ``auto_now_add`` field behaviour during flush. Since targeted flush
saves now use ``update_fields``, this setting only has effect for
whole-object (``func_name=NULL``) flushes triggered by :func:`mark_dirty`
or ``rebuildall``/``rebuild_instances_of``.


Upgrading
=========

After upgrading to 1.12, run ``manage.py denorm_rebuild_triggers`` — the
trigger SQL changed shape (per-function markers replace the old catch-all).
Targeted flush saves use ``update_fields`` and no longer touch ``auto_now``
columns; ``DENORM_DISABLE_AUTOTIME_DURING_FLUSH`` now only matters for
whole-object (NULL) flushes.
