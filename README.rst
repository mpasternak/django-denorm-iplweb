
.. image:: https://github.com/mpasternak/django-denorm-iplweb/actions/workflows/tests.yml/badge.svg

django-denorm-iplweb is a Django application to provide automatic management of denormalized database fields.

This is a fork of original package, that went by name of django-denorm_ . This fork should bring original
package to the latest Django/Python versions. Also, support for pretty much anything that is not
PostgreSQL was dropped.

Python versions supported: 3.8-3.10

Django versions supported: 4-5.

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
