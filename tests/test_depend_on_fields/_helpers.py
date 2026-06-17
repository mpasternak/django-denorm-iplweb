"""Shared helpers for the @depend_on_fields test modules."""

from django.contrib.contenttypes.models import ContentType


def _markers(model, pk):
    from denorm.models import DirtyInstance

    ct = ContentType.objects.get_for_model(model)
    return DirtyInstance.objects.filter(content_type=ct, object_id=pk)


def _named_func(name):
    """A stand-in for a @denormalized function with a given name.

    __qualname__ matters too: the PG Trigger.name() builds the trigger
    name from func.__qualname__, and a nested test function would leak
    '<locals>' into it.
    """

    def func(self):
        return ""

    func.__name__ = name
    func.__qualname__ = name
    return func
