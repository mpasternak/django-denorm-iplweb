from django.contrib import contenttypes
from django.core.exceptions import FieldDoesNotExist

import denorm
from denorm.helpers import content_type_select_sql

from .base import DenormDependency, _qv


class DependOnFields(DenormDependency):
    """Same-model dependency: the decorated function reads the declared
    sibling columns of its own model instance (plain columns or other
    denormalized fields).

    Emits one targeted AFTER UPDATE trigger that fires only when a
    declared column changes, inserting a per-function dirty marker
    (content_type_id, object_id, '<function name>').

    An EMPTY declaration (``@depend_on_fields()``) means "this function
    reads no sibling columns" and emits no UPDATE trigger at all.
    The unconditional INSERT marker is emitted by CallbackDenorm for
    every function regardless of declarations.
    """

    def __init__(self, *field_names, func=None):
        self.field_names = tuple(field_names)
        self.func = func

    def resolve_attnames(self):
        """Map declared names (field name or attname) to column attnames.

        Raises FieldDoesNotExist for unknown names, ValueError for a
        self-dependency on the function's own field.
        """
        concrete = list(self.this_model._meta.concrete_fields)
        by_either_name = {}
        for f in concrete:
            by_either_name[f.name] = f.attname
            by_either_name[f.attname] = f.attname

        own = self.func.__name__
        resolved = []
        for name in self.field_names:
            if name == own:
                raise ValueError(
                    f"@depend_on_fields on {self.this_model.__name__}.{own} "
                    f'declares its own field "{name}" — a denormalized '
                    f"function cannot depend on itself."
                )
            try:
                resolved.append(by_either_name[name])
            except KeyError:
                available = ", ".join(sorted({f.attname for f in concrete}))
                raise FieldDoesNotExist(
                    f'Field name "{name}", declared in @depend_on_fields of '
                    f"{self.this_model.__name__}.{own}, does not exist. "
                    f"Field names available: {available}"
                )
        # Deduplicate while preserving declaration order so that e.g.
        # @depend_on_fields("author", "author_id") doesn't produce a
        # duplicated trigger condition for the same attname "author_id".
        return list(dict.fromkeys(resolved))

    def get_triggers(self, using):
        if not self.field_names:
            return []

        from denorm.db import triggers

        attnames = self.resolve_attnames()
        qn = self.get_quote_name(using)
        content_type = str(
            contenttypes.models.ContentType.objects.get_for_model(self.this_model).pk
        )
        # DirtyInstance markers resolve the content-type id at trigger-fire
        # time (immune to content-type renumbering); the bare ``content_type``
        # literal stays for the Trigger name/WHEN, which can't hold a subquery.
        content_type_value = content_type_select_sql(self.this_model)
        action = triggers.TriggerActionInsert(
            model=denorm.models.DirtyInstance,
            columns=("content_type_id", "object_id", "func_name"),
            values=(
                content_type_value,
                "NEW.%s" % qn(self.this_model._meta.pk.get_attname_column()[1]),
                _qv(self.func.__name__),
            ),
        )
        # Note: Trigger.__init__ also merges the model's denorm_always_skip /
        # denorm_always_only into the watch-list.  If those model-level
        # settings exclude every column declared here, Trigger raises
        # ImproperlyConfigured (whose message does not mention
        # denorm_always_skip — this comment is the breadcrumb).
        return [
            triggers.Trigger(
                self.this_model,
                "after",
                "update",
                [action],
                content_type,
                using,
                None,
                tuple(attnames),
                self.func,
            )
        ]
