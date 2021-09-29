
django-denorm-iplweb is a Django application to provide automatic management of denormalized database fields.

This is a fork of original package, that went by name of django-denorm_ . This fork should bring original
package to the latest Django/Python versions. Also, support for pretty much anything that is not
PostgreSQL was dropped.

Python versions supported:

* 3.8,
* 3.9.

Django versions supported:

* 3.0,
* 3.1,
* 3.2.

Reasons for this fork being PostgreSQL-only:

* lack of resources for maintaning other backends,
* planned usage of ``LISTEN``/``NOTIFY`` mechanisms, available in PostgreSQL.

.. _django-denorm: https://github.com/django-denorm/django-denorm

Documentation is available from http://django-denorm-iplweb.github.io/django-denorm-iplweb/

Issues can be reported at http://github.com/mpasternak/django-denorm-iplweb/issues

.. image:: https://travis-ci.org/mpasternak/django-denorm-iplweb.svg?branch=develop
