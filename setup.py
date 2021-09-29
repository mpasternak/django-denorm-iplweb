#!/usr/bin/python

from setuptools import setup

setup(
    name='django-denorm-iplweb',
    version='1.0.0',
    description='Denormalization magic for Django',
    long_description='django-denorm-iplweb is a Django application to provide automatic management of denormalized database fields.',
    author_email='michal.dtz@gmail.com',
    url='http://github.com/mpasternak/django-denorm-iplweb/',
    classifiers=[
        "Development Status :: 2 - Pre-Alpha",
        "Framework :: Django",
        "Intended Audience :: Developers",
        "Intended Audience :: System Administrators",
        "Operating System :: OS Independent",
        "Topic :: Software Development"
    ],
    packages=[
        'denorm',
        'denorm.db',
        'denorm.db.postgresql',
        'denorm.management',
        'denorm.management.commands',
        'denorm.migrations',
    ],
)
