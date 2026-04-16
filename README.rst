
.. image:: https://github.com/mpasternak/django-denorm-iplweb/actions/workflows/tests.yml/badge.svg
   :target: https://github.com/mpasternak/django-denorm-iplweb/actions/workflows/tests.yml

django-denorm-iplweb is a Django application to provide automatic management of denormalized database fields.

This is a fork of original package, that went by name of django-denorm_ . This fork should bring original
package to the latest Django/Python versions. Also, support for pretty much anything that is not
PostgreSQL was dropped.

Supported versions:

+----------+------------+------------+------------+
|          | Django 4.2 | Django 5.2 | Django 6.0 |
|          | LTS        | LTS        |            |
+==========+============+============+============+
| Python   | |yes|      | |yes|      | |no|       |
| 3.10     |            |            |            |
+----------+------------+------------+------------+
| Python   | |yes|      | |yes|      | |no|       |
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

Documentation is available from http://django-denorm-iplweb.github.io/django-denorm-iplweb/

Issues can be reported at http://github.com/mpasternak/django-denorm-iplweb/issues
