Changelog
=========

1.10.2
------

* Django 6.0 compatibility: ``TriggerFilterQuery.JoinField`` implements
  ``get_joining_fields()`` alongside the legacy ``get_joining_columns()``.
  Removes ``RemovedInDjango60Warning: The usage of get_joining_columns()
  in Join is deprecated. Implement get_joining_fields() instead``.
* ``Field.pre_save()`` calls are now idempotent on Django 6.0.

1.10.1
------

* packaging migrated from ``setup.py``/``setup.cfg`` to ``pyproject.toml``
  with ``uv``,
* CI updated for Python 3.10-3.13 and Django 4.2/5.2 LTS,
* GitHub Actions upgraded to Node.js 24-compatible versions; CI badge
  link added to README,
* README refreshed with current Python/Django versions, license info
  and a support matrix,
* pre-commit: added ``detect-private-key`` hook, ``pyupgrade`` bumped
  to ``--py310-plus``,
* ``.gitignore`` expanded for modern Python development,
* Django 5 compatibility: removed ``NullBooleanField`` and deprecated
  settings.

1.10.0
------

* fixes for a race condition in queue processing.

1.9.0
-----

* switched from ``SKIP LOCKED`` to ``NOWAIT`` when acquiring row locks
  to fail fast instead of silently skipping rows.

1.8.0
-----

* optimizations around the ``NOWAIT`` locking path.

1.7.0
-----

* narrowed the scope of row-level locks during queue flushing to
  reduce contention.

1.6.0
-----

* another attempt at fixing the recurring deadlock/lock contention
  issues when multiple ``denorm_queue`` workers run in parallel.

1.5.0
-----

* skip similar, already-locked items when flushing to avoid deadlocks.

1.4.1
-----

* further fixes for skipping already-locked similar items to avoid
  deadlocks.

1.4.0
-----

* integration with ``celery-singleton`` to prevent duplicate Celery
  tasks from running simultaneously.

1.3.0
-----

* switched back to non-parametrized ``NOTIFY``,
* triggers emit ``NOTIFY`` only on ``AFTER STATEMENT`` to wake up the
  flusher rather than on every row change.

1.2.2
-----

* Celery tasks default to ignored results,
* queue flushing pushed to the backend (Celery worker),
* small fixes: added type to a parameter, corrected ``__str__``.

1.2.1
-----

* new management command to flush via Celery queues,
* pop all pending NOTIFY messages before flushing.

1.2.0
-----

* queue code refactored to use Celery.

1.1.5
-----

* attempt to fix recurring deadlock issues.

1.1.4
-----

* attempt to fix an issue with handling deletes.

1.1.3
-----

* further attempts at fixing deadlocks.

1.1.2
-----

* added ``skip_locked=True`` to avoid deadlocks,
* Django 4.2 and 5.0 support,
* tested against Python 3.11 and 3.12,
* test app migrations and general housekeeping,
* test fixes.

1.1.1
-----

* updated for Django 4.2 (work in progress),
* enabled more Django configurations in CI.

1.1.0
-----

* fixed ``NullBooleanField`` handling,
* enabled parallel ``tox`` testing,
* migrated CI from Travis to GitHub Actions,
* temporarily disabled Django 4.0,
* assorted build and link fixes.

1.0.0
-----

* try to avoid deadlocks during queue processing,
* first stable 1.x release.

0.5.5
-----

* changes to reduce the chance of multiple ``denorm_queue`` processes trying
  to denormalize the same object

0.5.4
-----

* don't wait for content_object when flushing queue, so we won't get deadlocks and
  Django exceptions

0.5.3
-----

* select_for_update also for the updated object, so we won't get deadlocks

0.5.2
-----

* include missing ``conf`` package.

0.5.1
-----

* optimized denorms.rebuildall, using bulk_create,
* denorm_rebuild command gets 2 new command-line options, model_name and no_flush,
* ability to disable auto_now_add and auto_now fields during denorm flush, using
  settings -- DENORM_DISABLE_AUTOTIME_DURING_FLUSH and field names
  DENORM_AUTOTIME_FIELD_NAMES,
* denorms.flush works in batches now.

0.5.0
-----

* first release of django-denorm-iplweb,
* based on the high-quality code of the original django-denorm_
* supported versions: Python 3.8, 3.9, Django 3.0, 3.1, 3.2,
* dropped support for MySQL,
* dropped support for SQLite,
* denorm_daemon becomes denorm_queue:
  - removed daemonzation code,
  - documented need to use supervisord or similar if background process needed,
  - used LISTEN/NOTIFY mechanisms from PostgreSQL,
* removed six dependency and __unicode__,
* added pre-commit hooks for autopep, flake8,
* added bumpver configuration,
* automatic trigger installation after post_migrate,
* documentation updated,
* post_migration signal causes trigger rebuild,
* ``rebuild_triggers`` command to rebuild triggers,
* deprecated command ``denormalize`` removed,
* field names given as a parameter to ``skip`` or ``denorm_always_skip`` are checked if they exist,
* triggers and functions names, generated for ``@depend_on_related`` include function (attribute) name,
* DirtyInstance includes func_name, which is a function name to rebuild only this single parameter
* ability to run multiple ``denorm_queue`` commands, which (thanks to the magic of row locking) should
  automatically process queue in a paralell manner.


.. _django-denorm: https://github.com/django-denorm/django-denorm
