Changelog
=========

1.12.0 (unreleased)
-------------------

* fix: ``flush_single`` now deletes claimed ``DirtyInstance`` markers at
  claim time. Previously, the unique-index dedup could silently swallow
  concurrent invalidation markers inserted between the claim and the
  delete, permanently losing them until the next full rebuild.
* feature: ``@depend_on_fields(*names)`` — declarative same-model
  dependencies for ``@denormalized`` functions. A declared function gets
  a targeted per-function database trigger that fires only when a
  declared column changes. ``@depend_on_fields()`` with no arguments
  means "reads no sibling columns" and emits no UPDATE self-trigger for
  that function (the INSERT marker and any ``@depend_on_related``
  triggers remain).
* Per-function triggers replace the old catch-all self-trigger: the
  library's triggers no longer emit ``func_name=NULL`` markers.
  Conservative (undeclared) functions get an any-watched-column trigger
  that excludes the function's own column. Consequence: bulk-writing a
  denormalized column directly is unsupported and does not mark anything
  dirty; write source columns instead or call ``mark_dirty()``.
* ``NULL`` contract: ``func_name=NULL`` means "recompute every
  denormalized field of this object" and takes precedence over
  field-level markers during flush. Only ``mark_dirty(*instances)`` and
  ``rebuildall``/``rebuild_instances_of`` produce NULL markers now.
* feature: ``denorm.mark_dirty(*instances)`` — explicitly marks whole
  objects dirty (NULL markers).
* feature: ``DENORM_MAX_FLUSH_PASSES`` setting (default ``100``):
  ``flush()`` aborts with an error log naming the still-dirty models
  instead of looping forever when a denormalized function is
  non-deterministic.
* feature: Django system checks ``denorm.E001``, ``denorm.E002``,
  ``denorm.W001``, ``denorm.W002`` audit ``@depend_on_fields``
  declarations using an AST scanner. An incomplete declaration causes
  silent staleness, exactly like a missing ``@depend_on_related``.
  Silence individual checks via ``SILENCED_SYSTEM_CHECKS``. PK reads
  are ignored.
* Targeted flush saves now use ``update_fields`` — ``auto_now`` columns
  are no longer touched by targeted flushes.
  ``DENORM_DISABLE_AUTOTIME_DURING_FLUSH`` now only applies to
  whole-object (``func_name=NULL``) flushes.
* **Upgrade note**: run ``manage.py denorm_rebuild_triggers`` after
  upgrading — trigger SQL changed shape (per-function markers).

1.11.1
------

* fix: ``denorm.tasks.flush_single`` is now keyed by the logical
  ``(content_type_id, object_id)`` pair instead of a representative
  ``DirtyInstance`` pk. Previously, if the captured marker was deleted
  by a concurrent flush path between ``flush_via_queue`` enqueue and
  task execution, and a fresh marker for the same pair was inserted in
  the meantime, the queued task aborted on ``DoesNotExist`` and the new
  marker was orphaned until the next flush cycle.
* fix: ``denorm/templates/denorm/dirty_instances_count.html`` is now
  included in the built wheel via ``[tool.setuptools.package-data]``.
  Without it, ``dirty_instances_count`` raised ``TemplateDoesNotExist``
  for users installing from PyPI.
* CI: ``tests/test_deadlocks.py`` (deadlock and race-regression suite)
  is now executed by ``tox`` alongside the existing ``test_app`` suite.

1.11.0
------

* Fix race conditions and deadlock handling in the flush pipeline
  (retry on serialization failures, deletion-by-pk after
  ``select_for_update``, deduplicated subtask fan-out, no global
  ``Field.auto_now`` mutation during flush).
* Add ``dirty_instances_count`` view with a configurable access policy
  (``DENORM_DIRTY_INSTANCES_VIEW_ACCESS`` = ``staff`` / ``authenticated``
  / ``public``).

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
