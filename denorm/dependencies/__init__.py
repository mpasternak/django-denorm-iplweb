"""Dependency resolvers for denormalized fields.

This package was split out of the former ``denorm/dependencies.py`` module.
The public names are re-exported here so that
``from denorm.dependencies import X`` and ``denorm.dependencies.X`` keep
working unchanged.
"""

from .base import DenormDependency, DependOnRelated, _qv
from .cachekey import CacheKeyDependOnRelated
from .callback import CallbackDependOnRelated
from .decorators import depend_on_fields, depend_on_related, make_depend_decorator
from .onfields import DependOnFields

__all__ = [
    "_qv",
    "DenormDependency",
    "DependOnRelated",
    "CacheKeyDependOnRelated",
    "CallbackDependOnRelated",
    "DependOnFields",
    "make_depend_decorator",
    "depend_on_related",
    "depend_on_fields",
]
