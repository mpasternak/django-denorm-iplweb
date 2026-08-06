from django.apps import apps
from django.db import connection, connections


def many_to_many_pre_save(sender, instance, **kwargs):
    """
    Updates denormalised many-to-many fields for the model
    """
    if instance.pk:
        # Need a primary key to do m2m stuff
        for m2m in sender._meta.local_many_to_many:
            # This gets us all m2m fields, so limit it to just those that are denormed
            if hasattr(m2m, "denorm"):
                # Does some extra jiggery-pokery for "through" m2m models.
                # May not work under lots of conditions.
                remote = m2m.remote_field
                if hasattr(remote, "through_model"):
                    # Clear exisiting through records (bit heavy handed?)
                    kwargs = {m2m.related.var_name: instance}

                    # Can't use m2m_column_name in a filter
                    # kwargs = { m2m.m2m_column_name(): instance.pk, }
                    remote.through_model.objects.filter(**kwargs).delete()

                    values = m2m.denorm.func(instance)
                    for value in values:
                        kwargs.update({m2m.m2m_reverse_name(): value.pk})
                        remote.through_model.objects.create(**kwargs)

                else:
                    values = m2m.denorm.func(instance)
                    getattr(instance, m2m.attname).set(values)


def many_to_many_post_save(sender, instance, created, **kwargs):
    if created:

        def check_resave():
            for m2m in sender._meta.local_many_to_many:
                if hasattr(m2m, "denorm"):
                    return True
            return False

        if check_resave():
            instance.save()


def get_alldenorms():
    """
    Get all denormalizations.
    """
    alldenorms = []
    for model in apps.get_models(include_auto_created=True):
        if not model._meta.proxy:
            for field in model._meta.fields:
                if hasattr(field, "denorm"):
                    if not field.denorm.model._meta.swapped:
                        alldenorms.append(field.denorm)
    return alldenorms


class Denorm:
    def __init__(self, skip=None, only=None):
        self.func = None
        self.skip = skip
        self.only = only

    def get_connection(self, using):
        if using:
            return connections[using]
        return connection

    def get_quote_name(self, using):
        return self.get_connection(using).ops.quote_name

    def setup(self, **kwargs):
        """
        Adds 'self' to the global denorm list
        and connects all needed signals.
        """

    def get_triggers(self, using):
        return []
