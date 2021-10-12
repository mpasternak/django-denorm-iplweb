from django.core.exceptions import FieldDoesNotExist, ImproperlyConfigured
from django.db import connection, connections, models

from ..helpers import remote_field_model


class RandomBigInt(object):
    def sql(self):
        raise NotImplementedError


class TriggerNestedSelect:
    def __init__(self, table, columns, **kwargs):
        self.table = table
        self.columns = ", ".join(columns)
        self.kwargs = kwargs

    def sql(self):
        raise NotImplementedError


class TriggerAction(object):
    def __init__(self):
        pass

    def sql(self):
        pass


class TriggerActionInsert(TriggerAction):
    def __init__(self, model, columns, values):
        self.model = model
        self.columns = columns
        self.values = values

    def __repr__(self):
        return (
            f"<TriggerActionInsert object at {id(self):#02x} "
            f"INSERT INTO {self.model}({self.columns}) values({self.values})>"
        )

    def sql(self):
        raise NotImplementedError


class TriggerActionUpdate(TriggerAction):
    def __init__(self, model, columns, values, where):
        self.model = model
        self.columns = columns
        self.where = where

        self.values = []
        for value in values:
            if hasattr(value, "sql"):
                self.values.append(value.sql())
            else:
                self.values.append(value)

    def sql(self):
        raise NotImplementedError


def get_fields_with_model(model, meta):
    try:
        return [
            (f, f.model if f.model != model else None)
            for f in meta.get_fields()
            if not f.is_relation or f.one_to_one or (f.many_to_one and f.related_model)
        ]
    except AttributeError:
        return meta.get_fields_with_model()


class Trigger(object):
    def __repr__(self):
        return (
            f"{object.__repr__(self)} for model {self.subject}, {self.time} {self.event} "
            f"perform {[str(action) for action in self.actions]}"
        )

    def __init__(
        self,
        subject,
        time,
        event,
        actions,
        content_type,
        using=None,
        skip=None,
        only=None,
        func=None,
    ):
        self.subject = subject
        self.time = time
        self.event = event
        self.content_type = content_type
        self.content_type_field = None
        self.actions = []
        self.append(actions)
        self.using = using
        self.func = func

        if self.using:
            self.connection = connections[self.using]
        else:
            self.connection = connection

        from django.contrib.contenttypes.fields import GenericRelation

        fields_with_model = None

        if isinstance(subject, models.ManyToManyField):
            self.model = None
            self.db_table = subject.m2m_db_table()
            self.fields = [
                (subject.m2m_column_name(), ""),
                (subject.m2m_reverse_name(), ""),
            ]

        elif isinstance(subject, GenericRelation):
            self.model = None
            self.db_table = remote_field_model(subject)._meta.db_table
            self.fields = [
                (k.attname, k.db_type(connection=self.connection))
                for k, v in get_fields_with_model(
                    remote_field_model(subject), remote_field_model(subject)._meta
                )
                if not v
            ]
            self.content_type_field = subject.content_type_field_name + "_id"

        elif isinstance(subject, models.ForeignKey):
            self.model = subject.model
            self.db_table = self.model._meta.db_table
            skip = (skip or ()) + getattr(self.model, "denorm_always_skip", ())
            only = (only or ()) + getattr(self.model, "denorm_always_only", ())
            fields_with_model = get_fields_with_model(subject.model, self.model._meta)
            field_names_with_model = [k.attname for k, v in fields_with_model]

            if only:
                # ``only`` parametr was given and it is expected to be an iterable of field
                # names, which will be watched for updates (an opposite to ``skip`` parameter).
                # Let's see if every single field name passed actually exists:
                for field_name in only:
                    if field_name not in field_names_with_model:
                        raise FieldDoesNotExist(
                            f'Field name "{field_name}", passed as a parameter to "only" to a trigger for class or '
                            f'included in class\' "denorm_always_only" for class {self.subject}, '
                            f'does not exist. Field names available: {", ".join(field_names_with_model)}'
                        )
            else:
                # not only
                only = field_names_with_model

            self.fields = [
                (k.attname, k.db_type(connection=self.connection))
                for k, v in fields_with_model
                if not v and k.attname not in skip and k.attname in only
            ]

        elif hasattr(subject, "_meta"):
            self.model = subject
            self.db_table = self.model._meta.db_table
            # FIXME: need to check get_parent_list and add triggers to those
            # The below will only check the fields on *this* model, not parents
            skip = (skip or ()) + getattr(self.model, "denorm_always_skip", ())
            only = (only or ()) + getattr(self.model, "denorm_always_only", ())

            self.fields = []

            from django.db.models.fields.related import ForeignObjectRel

            fields_with_model = get_fields_with_model(subject, self.model._meta)
            field_names_with_model = [
                k.attname
                for k, v in fields_with_model
                if not isinstance(k, ForeignObjectRel)
            ]
            if only:
                # ``only`` parametr was given and it is expected to be an iterable of field
                # names, which will be watched for updates (an opposite to ``skip`` parameter).
                # Let's see if every single field name passed actually exists:
                for field_name in only:
                    if field_name not in field_names_with_model:
                        raise FieldDoesNotExist(
                            f'Field name "{field_name}", passed as a parameter to "only" to a trigger for class or '
                            f'included in class\' "denorm_always_only" for class {self.subject}, '
                            f'does not exist. Field names available: {", ".join(field_names_with_model)}'
                        )
            else:
                # not only
                only = field_names_with_model

            for k, v in fields_with_model:
                if isinstance(k, ForeignObjectRel):
                    pass
                else:
                    if not v and k.attname not in skip and k.attname in only:

                        self.fields.append(
                            (k.attname, k.db_type(connection=self.connection))
                        )

        else:
            raise NotImplementedError

        if skip and fields_with_model:
            field_names = [k.attname for k, v in fields_with_model]
            # Check all fields given as a parameter to skip or denorm_always_skip
            # to see if they exist:
            for field_name in skip:
                if field_name not in field_names:
                    raise FieldDoesNotExist(
                        f'Field name "{field_name}", passed as a parameter to "skip" to a trigger for class or '
                        f'included in class\' "denorm_always_skip" for class {self.subject}, '
                        f'does not exist. Field names available: {", ".join(field_names)}'
                    )

        # Check if any fields left:
        if not self.fields:
            raise ImproperlyConfigured(
                f"It looks like there are no field names defined for a trigger defined for class {self.subject}. "
                f'This situation could occur if you excluded all of the fields using "only" parameter. '
                f'Please give less broad range of fields to "only" or use "skip" instead. '
            )

    def append(self, actions):
        if not isinstance(actions, list):
            actions = [actions]

        for action in actions:
            self.actions.append(action)

    def name(self):
        return "_".join(
            ["d", self.time[:3], "row", self.event[:3], "on", self.db_table]
        )

    def sql(self):
        raise NotImplementedError


class TriggerSet(object):
    def __init__(self, using=None):
        self.using = using
        self.triggers = {}
        if self.using:
            self.connection = connections[self.using]
        else:
            self.connection = connection

    def cursor(self):
        return self.connection.cursor()

    def append(self, triggers):
        if not isinstance(triggers, list):
            triggers = [triggers]

        for trigger in triggers:
            name = trigger.name()
            if name in self.triggers:
                self.triggers[name].append(trigger.actions)
            else:
                self.triggers[name] = trigger

    def install(self):
        raise NotImplementedError

    def drop(self):
        raise NotImplementedError
