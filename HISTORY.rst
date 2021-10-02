Changelog
=========

0.5.0
-----

* first release of django-denorm-iplweb,
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
* field names given as a parameter to ``skip`` or ``denorm_always_skip`` are checked if they exist.
