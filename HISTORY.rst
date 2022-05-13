Changelog
=========

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
