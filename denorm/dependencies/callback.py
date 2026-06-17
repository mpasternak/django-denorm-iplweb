from django.contrib import contenttypes
from django.db import models

import denorm
from denorm.helpers import content_type_select_sql, remote_field_model

from .base import DependOnRelated, _qv


class CallbackDependOnRelated(DependOnRelated):

    """
    A DenormDependency that handles callbacks depending on fields
    in other models that are related to the dependent model.

    Two models are considered related if there is a ForeignKey or ManyToManyField
    on either of them pointing to the other one.
    """

    def __init__(
        self,
        othermodel,
        foreign_key=None,
        type=None,
        skip=None,
        only=None,
        func=None,
    ):
        """
        Attaches a dependency to a callable, indicating the return value depends on
        fields in an other model that is related to the model the callable belongs to
        either through a ForeignKey in either direction or a ManyToManyField.

        **Arguments:**

        othermodel (required)
            Either a model class or a string naming a model class.

        foreign_key
            The name of the ForeignKey or ManyToManyField that creates the relation
            between the two models.
            Only necessary if there is more than one relationship between the two models.

        type
            One of 'forward', 'backward', 'forward_m2m' or 'backward_m2m'.
            If there are relations in both directions specify which one to use.

        skip
            Use this to specify what fields change on every save().
            These fields will not be checked and will not make a model dirty when they change, t
            o prevent infinite loops.

        only
            Use this to specify what fields should be watched instead of every field on save().
            Only those fields will be checked. Opposite of ``skip``.

        func
            Reference to function, that this is a callback to.

        """
        super(CallbackDependOnRelated, self).__init__(
            othermodel, foreign_key, type, skip=skip, only=only, func=func
        )

    def get_triggers(self, using):
        from denorm.db import triggers

        qn = self.get_quote_name(using)

        if not self.type:
            # 'resolved_model' model never got called...
            raise ValueError(
                "The model '%s' could not be resolved, it probably does not exist"
                % self.other_model
            )

        content_type = str(
            contenttypes.models.ContentType.objects.get_for_model(self.this_model).pk
        )
        # DirtyInstance markers resolve the content-type id at trigger-fire
        # time (immune to content-type renumbering); the bare ``content_type``
        # literal stays for the Trigger name/WHEN, which can't hold a subquery.
        content_type_value = content_type_select_sql(self.this_model)

        if self.type == "forward":
            # breakpoint()
            # With forward relations many instances of ``this_model``
            # may be related to one instance of ``other_model``
            # so we need to do a nested select query in the trigger
            # to find them all.
            action_new = triggers.TriggerActionInsert(
                model=denorm.models.DirtyInstance,
                columns=("content_type_id", "object_id", "func_name"),
                values=triggers.TriggerNestedSelect(
                    self.this_model._meta.pk.model._meta.db_table,
                    (
                        content_type_value,
                        self.this_model._meta.pk.get_attname_column()[1],
                        _qv(self.func.__name__),
                    ),
                    **{
                        self.field.get_attname_column()[1]: "NEW.%s"
                        % qn(self.other_model._meta.pk.get_attname_column()[1])
                    },
                ),
            )
            action_old = triggers.TriggerActionInsert(
                model=denorm.models.DirtyInstance,
                columns=("content_type_id", "object_id", "func_name"),
                values=triggers.TriggerNestedSelect(
                    self.this_model._meta.pk.model._meta.db_table,
                    (
                        content_type_value,
                        self.this_model._meta.pk.get_attname_column()[1],
                        _qv(self.func.__name__),
                    ),
                    **{
                        self.field.get_attname_column()[1]: "OLD.%s"
                        % qn(self.other_model._meta.pk.get_attname_column()[1])
                    },
                ),
            )
            return [
                triggers.Trigger(
                    self.other_model,
                    "after",
                    "update",
                    [action_new],
                    content_type,
                    using,
                    self.skip,
                    self.only,
                    self.func,
                ),
                triggers.Trigger(
                    self.other_model,
                    "after",
                    "insert",
                    [action_new],
                    content_type,
                    using,
                    self.skip,
                    self.only,
                    self.func,
                ),
                triggers.Trigger(
                    self.other_model,
                    "after",
                    "delete",
                    [action_old],
                    content_type,
                    using,
                    self.skip,
                    self.only,
                    self.func,
                ),
            ]

        if self.type == "backward":
            # With backward relations a change in ``other_model`` can affect
            # only one or two instances of ``this_model``.
            # If the ``other_model`` instance changes the value its ForeignKey
            # pointing to ``this_model`` both the old and the new related instance
            # are affected, otherwise only the one it is pointing to is affected.
            action_new = triggers.TriggerActionInsert(
                model=denorm.models.DirtyInstance,
                columns=("content_type_id", "object_id", "func_name"),
                values=triggers.TriggerNestedSelect(
                    self.field.model._meta.db_table,
                    (
                        content_type_value,
                        self.field.get_attname_column()[1],
                        _qv(self.func.__name__),
                    ),
                    **{
                        self.field.model._meta.pk.get_attname_column()[1]: "NEW.%s"
                        % qn(self.other_model._meta.pk.get_attname_column()[1])
                    },
                ),
            )
            action_old = triggers.TriggerActionInsert(
                model=denorm.models.DirtyInstance,
                columns=("content_type_id", "object_id", "func_name"),
                values=(
                    content_type_value,
                    "OLD.%s" % self.field.get_attname_column()[1],
                    _qv(self.func.__name__),
                ),
            )
            return [
                triggers.Trigger(
                    self.other_model,
                    "after",
                    "update",
                    [action_new, action_old],
                    content_type,
                    using,
                    self.skip,
                    self.only,
                    self.func,
                ),
                triggers.Trigger(
                    self.other_model,
                    "after",
                    "insert",
                    [action_new],
                    content_type,
                    using,
                    self.skip,
                    self.only,
                    self.func,
                ),
                triggers.Trigger(
                    self.other_model,
                    "after",
                    "delete",
                    [action_old],
                    content_type,
                    using,
                    self.skip,
                    self.only,
                    self.func,
                ),
            ]

        if "m2m" in self.type:
            # The two directions of M2M relations only differ in the column
            # names used in the intermediate table.
            if isinstance(self.field, models.ManyToManyField):
                if "forward" in self.type:
                    column_name = qn(self.field.m2m_column_name())
                    reverse_column_name = self.field.m2m_reverse_name()
                if "backward" in self.type:
                    column_name = qn(self.field.m2m_reverse_name())
                    reverse_column_name = self.field.m2m_column_name()
            else:
                if "forward" in self.type:
                    column_name = qn(self.field.object_id_field_name)
                    reverse_column_name = remote_field_model(self.field)._meta.pk.column
                if "backward" in self.type:
                    column_name = qn(remote_field_model(self.field)._meta.pk.column)
                    reverse_column_name = self.field.object_id_field_name

            # The first part of a M2M dependency is exactly like a backward
            # ForeignKey dependency. ``this_model`` is backward FK related
            # to the intermediate table.
            action_m2m_new = triggers.TriggerActionInsert(
                model=denorm.models.DirtyInstance,
                columns=("content_type_id", "object_id", "func_name"),
                values=(
                    content_type_value,
                    "NEW.%s" % column_name,
                    _qv(self.func.__name__),
                ),
            )
            action_m2m_old = triggers.TriggerActionInsert(
                model=denorm.models.DirtyInstance,
                columns=("content_type_id", "object_id", "func_name"),
                values=(
                    content_type_value,
                    "OLD.%s" % column_name,
                    _qv(self.func.__name__),
                ),
            )

            trigger_list = [
                triggers.Trigger(
                    self.field,
                    "after",
                    "update",
                    [action_m2m_new, action_m2m_old],
                    content_type,
                    using,
                    self.skip,
                    self.only,
                    self.func,
                ),
                triggers.Trigger(
                    self.field,
                    "after",
                    "insert",
                    [action_m2m_new],
                    content_type,
                    using,
                    self.skip,
                    self.only,
                    self.func,
                ),
                triggers.Trigger(
                    self.field,
                    "after",
                    "delete",
                    [action_m2m_old],
                    content_type,
                    using,
                    self.skip,
                    self.only,
                    self.func,
                ),
            ]

            if isinstance(self.field, models.ManyToManyField):
                # Additionally to the dependency on the intermediate table
                # ``this_model`` is dependant on updates to the ``other_model``-
                # There is no need to track insert or delete events here,
                # because a relation can only be created or deleted by
                # by modifying the intermediate table.
                #
                # Generic relations are excluded because they have the
                # same m2m_table and model table.
                action_new = triggers.TriggerActionInsert(
                    model=denorm.models.DirtyInstance,
                    columns=("content_type_id", "object_id", "func_name"),
                    values=triggers.TriggerNestedSelect(
                        self.field.m2m_db_table(),
                        (content_type_value, column_name, _qv(self.func.__name__)),
                        **{
                            reverse_column_name: "NEW.%s"
                            % qn(self.other_model._meta.pk.get_attname_column()[1])
                        },
                    ),
                )
                trigger_list.append(
                    triggers.Trigger(
                        self.other_model,
                        "after",
                        "update",
                        [action_new],
                        content_type,
                        using,
                        self.skip,
                        self.only,
                        self.func,
                    )
                )

            return trigger_list

        return []
