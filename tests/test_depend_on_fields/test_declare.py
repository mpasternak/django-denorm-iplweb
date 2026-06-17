"""@depend_on_fields declaration: validation, trigger shape, decorator."""

from __future__ import annotations

import pytest

from django.core.exceptions import FieldDoesNotExist

from ._helpers import _named_func


class TestDependOnFieldsValidation:
    def test_unknown_field_name_raises_at_trigger_build(self, db):
        from test_app.models import Member

        from denorm.dependencies import DependOnFields

        dep = DependOnFields("no_such_field", func=_named_func("full_name"))
        dep.setup(Member)
        with pytest.raises(FieldDoesNotExist) as exc:
            dep.get_triggers(using=None)
        assert "no_such_field" in str(exc.value)
        # The message must list available fields to be actionable.
        assert "first_name" in str(exc.value)

    def test_own_field_self_dependency_raises(self, db):
        from test_app.models import Member

        from denorm.dependencies import DependOnFields

        dep = DependOnFields("full_name", func=_named_func("full_name"))
        dep.setup(Member)
        with pytest.raises(ValueError) as exc:
            dep.get_triggers(using=None)
        assert "full_name" in str(exc.value)

    def test_empty_declaration_emits_no_triggers(self, db):
        from test_app.models import Member

        from denorm.dependencies import DependOnFields

        dep = DependOnFields(func=_named_func("full_name"))
        dep.setup(Member)
        assert dep.get_triggers(using=None) == []


class TestDependOnFieldsTriggerShape:
    def test_targeted_update_trigger(self, db):
        from test_app.models import Member

        from denorm.dependencies import DependOnFields

        dep = DependOnFields("first_name", "name", func=_named_func("full_name"))
        dep.setup(Member)
        triggers = dep.get_triggers(using=None)

        assert len(triggers) == 1
        trigger = triggers[0]
        assert trigger.event == "update"
        # Watch-list is exactly the declared columns.
        assert sorted(f for f, _ in trigger.fields) == ["first_name", "name"]
        # func is set -> per-function trigger name, never merged with others.
        assert "full_name" in trigger.name()

        sql, params = trigger.actions[0].sql()
        assert "func_name" in sql
        assert "'full_name'" in sql
        assert "NULL" not in sql.upper().replace("ON CONFLICT", "")

    def test_fk_field_name_normalised_to_attname(self, db):
        """resolve_attnames must map a FK field name ('forum') to its attname
        ('forum_id').  The scanner that consumes this list assumes attnames.
        """
        from test_app.models import Post

        from denorm.dependencies import DependOnFields

        dep = DependOnFields("forum", func=_named_func("forum_title"))
        dep.setup(Post)
        triggers = dep.get_triggers(using=None)

        assert len(triggers) == 1
        watch_fields = [f for f, _ in triggers[0].fields]
        assert watch_fields == ["forum_id"]


class TestDecorator:
    def test_decorator_attaches_dependency_info(self):
        from denorm.dependencies import DependOnFields, depend_on_fields

        @depend_on_fields("first_name", "last_name")
        def full_name(self):
            return ""

        assert len(full_name.depend) == 1
        cls, args, kwargs = full_name.depend[0]
        assert cls is DependOnFields
        assert args == ("first_name", "last_name")
        assert kwargs["func"] is full_name

    def test_exported_from_package_root(self):
        import denorm

        assert callable(denorm.depend_on_fields)
