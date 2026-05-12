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
