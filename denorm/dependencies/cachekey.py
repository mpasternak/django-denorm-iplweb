from django.contrib import contenttypes
from django.db import models

from .base import DependOnRelated


class CacheKeyDependOnRelated(DependOnRelated):
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

        if self.type == "forward":

            # With forward relations many instances of ``this_model``
            # may be related to one instance of ``other_model``
            action_new = triggers.TriggerActionUpdate(
                model=self.this_model,
                columns=(self.fieldname,),
                values=(triggers.RandomBigInt(),),
                where="%s = NEW.%s"
                % (
                    qn(self.field.get_attname_column()[1]),
                    qn(self.other_model._meta.pk.get_attname_column()[1]),
                ),
            )
            action_old = triggers.TriggerActionUpdate(
                model=self.this_model,
                columns=(self.fieldname,),
                values=(triggers.RandomBigInt(),),
                where="%s = OLD.%s"
                % (
                    qn(self.field.get_attname_column()[1]),
                    qn(self.other_model._meta.pk.get_attname_column()[1]),
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
            action_new = triggers.TriggerActionUpdate(
                model=self.this_model,
                columns=(self.fieldname,),
                values=(triggers.RandomBigInt(),),
                where="%s = NEW.%s"
                % (
                    qn(self.this_model._meta.pk.get_attname_column()[1]),
                    qn(self.field.get_attname_column()[1]),
                ),
            )
            action_old = triggers.TriggerActionUpdate(
                model=self.this_model,
                columns=(self.fieldname,),
                values=(triggers.RandomBigInt(),),
                where="%s = OLD.%s"
                % (
                    qn(self.this_model._meta.pk.get_attname_column()[1]),
                    qn(self.field.get_attname_column()[1]),
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
                    column_name = self.field.m2m_column_name()
                    reverse_column_name = self.field.m2m_reverse_name()
                if "backward" in self.type:
                    column_name = self.field.m2m_reverse_name()
                    reverse_column_name = self.field.m2m_column_name()
            else:
                if "forward" in self.type:
                    column_name = self.field.object_id_field_name
                    reverse_column_name = self.field.remote_field.model._meta.pk.column
                if "backward" in self.type:
                    column_name = self.field.remote_field.model._meta.pk.column
                    reverse_column_name = self.field.object_id_field_name

            # The first part of a M2M dependency is exactly like a backward
            # ForeignKey dependency. ``this_model`` is backward FK related
            # to the intermediate table.
            action_m2m_new = triggers.TriggerActionUpdate(
                model=self.this_model,
                columns=(self.fieldname,),
                values=(triggers.RandomBigInt(),),
                where="%s = NEW.%s"
                % (
                    qn(self.this_model._meta.pk.get_attname_column()[1]),
                    qn(column_name),
                ),
            )
            action_m2m_old = triggers.TriggerActionUpdate(
                model=self.this_model,
                columns=(self.fieldname,),
                values=(triggers.RandomBigInt(),),
                where="%s = OLD.%s"
                % (
                    qn(self.this_model._meta.pk.get_attname_column()[1]),
                    qn(column_name),
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
                sql, params = triggers.TriggerNestedSelect(
                    self.field.m2m_db_table(),
                    (column_name,),
                    **{
                        reverse_column_name: "NEW.%s"
                        % qn(self.other_model._meta.pk.get_attname_column()[1])
                    },
                ).sql()
                action_new = triggers.TriggerActionUpdate(
                    model=self.this_model,
                    columns=(self.fieldname,),
                    values=(triggers.RandomBigInt(),),
                    where=(
                        self.this_model._meta.pk.get_attname_column()[1]
                        + " IN ("
                        + sql
                        + ")",
                        params,
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
