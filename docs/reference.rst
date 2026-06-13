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

.. note::

   ``CountField`` and ``SumField`` are trigger-maintained: the database
   trigger increments or decrements the column atomically. The
   ``pre_save`` hook writes ``col = col`` (a Django ``F()`` expression) on
   UPDATE so it can never overwrite a concurrent trigger increment.
   ``save()`` leaves the in-memory attribute untouched (it keeps whatever
   value it had before the save). Call ``instance.refresh_from_db()``
   whenever you need the current counter value after a ``save()``.


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

``DENORM_SINGLETON_LOCK_EXPIRY`` (default ``600``): TTL in seconds for the
Redis lock held by ``celery-singleton``-based tasks (``flush_single``,
``flush_batch``, ``flush_via_queue``). Without an expiry a hard-killed worker
(OOM-kill, SIGKILL, power loss) leaves its lock forever and the affected
``(content_type_id, object_id)`` pair can never be re-enqueued. A flush that
legitimately runs longer than the expiry only allows a duplicate concurrent
task — safe because ``flush_single`` uses ``skip_locked`` claims.

``DENORM_QUEUE_CHUNK_SIZE`` (default ``50``): number of
``(content_type_id, object_id)`` pairs included in each ``flush_batch``
Celery task dispatched by ``flush_via_queue``. Increase if your broker or
worker startup overhead dominates; decrease for finer progress granularity.

``DENORM_MAX_CONVERGE_PASSES`` (default ``5``): per-object convergence pass
cap inside ``flush_single``. After each ``save()`` call, ``flush_single``
checks whether the save's own triggers inserted new markers for the same
``(content_type_id, object_id)`` pair (same-model denorm chains, e.g.
``full_name`` → ``letterhead``), and if so, processes them immediately in
the same transaction instead of deferring them to the next outer ``flush()``
pass. This setting caps the number of such inner iterations. On reaching the
cap, any remaining markers are left in the table and handled by the outer
``flush()`` loop (itself bounded by ``DENORM_MAX_FLUSH_PASSES``). The default
of ``5`` is above any realistic same-model chain depth; increase it only if
you have intentionally deep same-model chains. Degraded behavior (hitting the
cap) equals today's pre-1.12.0 behavior: correct eventual convergence, just
with extra outer passes.


Running the tests
=================

**Docker required.** The test suite spins two containers automatically:

* A **PostgreSQL** container (via ``testcontainers``) for the Django test
  database — always required, as this is a PostgreSQL-only package.
* A **Redis** container (``RedisContainer("redis:7-alpine")``) for the Celery
  broker, result backend, and ``celery-singleton`` lock backend.

Two test runners are provided:

``uv run pytest tests/ -q``
    The pytest suite (~59 tests). The ``celery_redis`` session fixture starts
    the Redis container once, injects the URL into the live Celery app, and
    sets ``task_always_eager = True`` so most tests run tasks inline. A small
    number of end-to-end tests use the ``live_worker`` fixture, which flips
    eager mode off and starts a real in-process Celery worker (``pool=solo``)
    for genuine broker serialization, round-trip dispatch, and
    ``flush_batch`` group fan-out. Singleton dedup is verified by direct
    lock-backend assertions against Redis (deterministic, avoids race
    conditions).

``uv run python run_tests_tc.py``
    The Django test runner suite (~43 tests). Starts both the Postgres and
    Redis containers, exports ``DENORM_TEST_REDIS_URL``, then delegates to
    ``manage.py test``.

No ``DENORM_TEST_REDIS_URL`` environment variable is needed when running the
suites through their respective runners — the containers are started and the
variable is set automatically. To run against an existing Redis instance,
set ``DENORM_TEST_REDIS_URL=redis://<host>:<port>/0`` before running either
suite.


Upgrading
=========

After upgrading to 1.12, run ``manage.py denorm_rebuild_triggers`` — the
trigger SQL changed shape (per-function markers replace the old catch-all).
Targeted flush saves use ``update_fields`` and no longer touch ``auto_now``
columns; ``DENORM_DISABLE_AUTOTIME_DURING_FLUSH`` now only matters for
whole-object (NULL) flushes.
