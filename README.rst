
.. image:: https://github.com/mpasternak/django-denorm-iplweb/actions/workflows/tests.yml/badge.svg
   :target: https://github.com/mpasternak/django-denorm-iplweb/actions/workflows/tests.yml

django-denorm-iplweb is a Django application to provide automatic management of denormalized database fields.

This is a fork of original package, that went by name of django-denorm_ . This fork should bring original
package to the latest Django/Python versions. Also, support for pretty much anything that is not
PostgreSQL was dropped.

Supported versions:

+----------+------------+------------+------------+
|          | Django 5.2 | Django 6.0 | Django 6.1 |
|          | LTS        |            |            |
+==========+============+============+============+
| Python   | |yes|      | |no|       | |no|       |
| 3.10     |            |            |            |
+----------+------------+------------+------------+
| Python   | |yes|      | |no|       | |no|       |
| 3.11     |            |            |            |
+----------+------------+------------+------------+
| Python   | |yes|      | |yes|      | |yes|      |
| 3.12     |            |            |            |
+----------+------------+------------+------------+
| Python   | |yes|      | |yes|      | |yes|      |
| 3.13     |            |            |            |
+----------+------------+------------+------------+

.. |yes| unicode:: U+2705
.. |no| unicode:: U+274C

Licensed under the BSD 3-Clause license.

Requires Celery.

Reasons for this fork being PostgreSQL-only:

* lack of resources for maintaning other backends,
* usage of ``LISTEN``/``NOTIFY`` mechanisms, available in PostgreSQL,
* many more improvements, for example the ability to run multiple instances of cache
  rebuilder (see docs_)

Patches welcome!

.. _django-denorm: https://github.com/django-denorm/django-denorm
.. _docs: https://django-denorm-iplweb.readthedocs.io/en/latest/history.html#id1

Deferring computation
=====================

Fields declared with ``@denormalized`` are computed synchronously when their
model is saved. To skip that for selected fields, e.g. while the Django admin
saves a parent object before its inlines, wrap the writes in
``denorm.deferred()``. The fields are then left untouched on UPDATE (or set
to their default on INSERT) and marked dirty for ``flush()`` on block exit::

    import denorm
    from django.db import transaction

    with transaction.atomic(), denorm.deferred("app.Article.summary"):
        article.save()
        for section in sections:
            section.save()

See the tutorial ("Deferring the computation") for details and limitations.

Dirty instances view
====================

The package ships with an optional view that displays the current number of
``DirtyInstance`` rows grouped by content type — useful for monitoring the
denormalization queue from a browser instead of running
``./manage.py denorm_show_dirtyinstances_count`` over SSH.

Wire it into your project's URLConf::

    # urls.py
    from django.urls import include, path

    urlpatterns = [
        ...
        path("denorm/", include("denorm.urls")),
    ]

The view is then reachable at ``/denorm/dirty-instances/`` and via
``reverse("denorm:dirty_instances_count")``.

Access policy is controlled by the ``DENORM_DIRTY_INSTANCES_VIEW_ACCESS``
setting (default: ``"staff"``):

* ``"staff"`` — only users with ``is_staff=True`` (uses
  ``staff_member_required``).
* ``"authenticated"`` — any logged-in user (uses ``login_required``).
* ``"public"`` — no access control. Only enable this behind your own
  network-level protection; the page leaks model names from your project.

Example::

    # settings.py
    DENORM_DIRTY_INSTANCES_VIEW_ACCESS = "authenticated"

Documentation is available from http://django-denorm-iplweb.github.io/django-denorm-iplweb/

Issues can be reported at http://github.com/mpasternak/django-denorm-iplweb/issues
