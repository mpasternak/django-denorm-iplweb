"""Tests for @depend_on_fields — declarative same-model dependencies.

Design: docs/superpowers/specs/2026-06-12-depend-on-fields-design.md
"""

from __future__ import annotations

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import FieldDoesNotExist


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


class TestPerFunctionSelfTriggers:
    def test_bulk_update_emits_targeted_markers_not_null(
        self, transactional_db, denorm_triggers
    ):
        from test_app.models import Profile

        from denorm.models import DirtyInstance

        p = Profile.objects.create(first_name="John", last_name="Doe")
        DirtyInstance.objects.all().delete()

        Profile.objects.filter(pk=p.pk).update(last_name="Smith")

        func_names = set(_markers(Profile, p.pk).values_list("func_name", flat=True))
        assert None not in func_names, (
            "Library trigger emitted a func_name=NULL (whole-object) marker. "
            "NULL is reserved for explicit mark_dirty()/rebuild."
        )
        assert "full_name" in func_names  # declared on last_name
        assert "letterhead" not in func_names  # declared only on full_name column

    def test_flush_recomputes_only_the_marked_field(
        self, transactional_db, denorm_triggers
    ):
        from test_app.models import Profile

        from denorm import denorms
        from denorm.models import DirtyInstance

        p = Profile.objects.create(first_name="John", last_name="Doe")
        denorms.flush()
        DirtyInstance.objects.all().delete()

        # Corrupt letterhead's column directly (no trigger watches it),
        # then dirty ONLY full_name via its declared source column.
        Profile.objects.filter(pk=p.pk).update(letterhead="SENTINEL")
        assert not DirtyInstance.objects.exists()
        Profile.objects.filter(pk=p.pk).update(last_name="Smith")

        ct = ContentType.objects.get_for_model(Profile)
        denorms.flush_single(ct.pk, p.pk, ct)

        p.refresh_from_db()
        assert p.full_name == "John Smith"
        assert p.letterhead == "SENTINEL", (
            "flush_single(update_fields=['full_name']) must not rewrite the "
            "letterhead column — targeted markers mean targeted saves."
        )

    def test_chain_cascades_and_full_flush_converges(
        self, transactional_db, denorm_triggers
    ):
        from test_app.models import Profile

        from denorm import denorms
        from denorm.models import DirtyInstance

        p = Profile.objects.create(first_name="John", last_name="Doe")
        denorms.flush()
        DirtyInstance.objects.all().delete()

        Profile.objects.filter(pk=p.pk).update(last_name="Smith")
        ct = ContentType.objects.get_for_model(Profile)

        # First targeted flush changes the full_name COLUMN, whose trigger
        # must cascade a 'letterhead' marker (its key is unclaimed, so it
        # lands even before the delete-at-claim fix).
        denorms.flush_single(ct.pk, p.pk, ct)
        cascade = set(_markers(Profile, p.pk).values_list("func_name", flat=True))
        assert "letterhead" in cascade

        denorms.flush()
        p.refresh_from_db()
        assert p.full_name == "John Smith"
        assert p.letterhead == "Dear John Smith"
        assert not DirtyInstance.objects.exists()

    def test_chain_converges_regardless_of_declaration_order(
        self, transactional_db, denorm_triggers
    ):
        from test_app.models import ProfileReversed

        from denorm import denorms
        from denorm.models import DirtyInstance

        p = ProfileReversed.objects.create(first_name="John", last_name="Doe")
        denorms.flush()
        DirtyInstance.objects.all().delete()

        ProfileReversed.objects.filter(pk=p.pk).update(last_name="Smith")
        denorms.flush()

        p.refresh_from_db()
        assert p.full_name == "John Smith"
        assert p.letterhead == "Dear John Smith", (
            "Chained denorm declared BEFORE its source stayed stale. "
            "Per-function markers must make convergence independent of "
            "field declaration order."
        )

    def test_raw_sql_insert_marks_every_function(
        self, transactional_db, denorm_triggers
    ):
        from django.db import connection

        from test_app.models import Profile

        from denorm import denorms

        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO test_app_profile "
                "(first_name, last_name, nickname, full_name, letterhead) "
                "VALUES ('Raw', 'Insert', '', '', '') RETURNING id"
            )
            pk = cursor.fetchone()[0]

        func_names = set(_markers(Profile, pk).values_list("func_name", flat=True))
        assert func_names == {"full_name", "letterhead"}

        denorms.flush()
        p = Profile.objects.get(pk=pk)
        assert p.full_name == "Raw Insert"
        assert p.letterhead == "Dear Raw Insert"

    def test_undeclared_function_gets_conservative_targeted_marker(
        self, transactional_db, denorm_triggers
    ):
        from test_app.models import UndeclaredProfile

        from denorm import denorms
        from denorm.models import DirtyInstance

        p = UndeclaredProfile.objects.create(first_name="John", last_name="Doe")
        denorms.flush()
        DirtyInstance.objects.all().delete()

        UndeclaredProfile.objects.filter(pk=p.pk).update(first_name="Jane")
        func_names = set(
            _markers(UndeclaredProfile, p.pk).values_list("func_name", flat=True)
        )
        assert func_names == {"full_name"}

        denorms.flush()
        p.refresh_from_db()
        assert p.full_name == "Jane Doe"
        assert not DirtyInstance.objects.exists()


class TestTriggerSetShape:
    def test_profile_triggerset_shape(self, db):
        from denorm.denorms import build_triggerset

        ts = build_triggerset()
        profile_triggers = {
            name: t
            for name, t in ts.triggers.items()
            if t.db_table == "test_app_profile"
        }

        updates = {n: t for n, t in profile_triggers.items() if t.event == "update"}
        inserts = {n: t for n, t in profile_triggers.items() if t.event == "insert"}

        # Two per-function UPDATE triggers (func-suffixed names).
        assert len(updates) == 2
        assert any("full_name" in n for n in updates)
        assert any("letterhead" in n for n in updates)
        # Watch-lists are exactly the declared columns.
        for name, t in updates.items():
            cols = sorted(f for f, _ in t.fields)
            if "letterhead" in name:
                assert cols == ["full_name"]
            else:
                assert cols == ["first_name", "last_name"]

        # ONE merged INSERT trigger carrying both functions' marker actions.
        assert len(inserts) == 1
        insert_trigger = next(iter(inserts.values()))
        action_sqls = [a.sql()[0] for a in insert_trigger.actions]
        assert any("'full_name'" in s for s in action_sqls)
        assert any("'letterhead'" in s for s in action_sqls)

    def test_conservative_trigger_excludes_own_column(self, db):
        """A conservative (undeclared) trigger must not watch the
        function's own column: flush writes that column, and watching it
        would let non-deterministic functions re-mark themselves forever
        once delete-at-claim lands (see plan AMENDMENT)."""
        from denorm.denorms import build_triggerset

        ts = build_triggerset()
        updates = [
            t
            for t in ts.triggers.values()
            if t.db_table == "test_app_undeclaredprofile" and t.event == "update"
        ]
        assert len(updates) == 1
        watched = {f for f, _ in updates[0].fields}
        assert "full_name" not in watched
        assert {"first_name", "last_name"} <= watched

    def test_self_trigger_never_merges_with_depend_on_related_self(self, db):
        """Post.response_count uses depend_on_related('self'): pins that the
        conservative self-trigger never merges with the dependency trigger.
        If they merged, the dependency trigger's response_count column-watch
        (needed to cascade markers to parent posts) would be silently lost —
        the silent-cascade-loss bug found during implementation."""
        from denorm.denorms import build_triggerset

        ts = build_triggerset()
        post_updates = {
            name: t
            for name, t in ts.triggers.items()
            if t.db_table == "test_app_post" and t.event == "update"
        }

        # Conservative self-trigger: marks response_count dirty on its own
        # row; must NOT watch response_count (flush writes that column).
        self_triggers = [
            t for n, t in post_updates.items() if n.endswith("response_count_self")
        ]
        assert len(self_triggers) == 1
        assert "response_count" not in {f for f, _ in self_triggers[0].fields}

        # Separate dependency trigger for depend_on_related('self'): DOES
        # watch response_count, to cascade markers to parent posts.
        dep_triggers = [
            t
            for n, t in post_updates.items()
            if "response_count" in n and not n.endswith("_self")
        ]
        assert len(dep_triggers) == 1
        assert "response_count" in {f for f, _ in dep_triggers[0].fields}

    def test_no_trigger_in_library_inserts_null_func_name(self, db):
        """Library-wide invariant: EVERY trigger action that inserts into
        DirtyInstance — on any table — must carry a func_name.  NULL
        (whole-object) markers are reserved for explicit
        mark_dirty()/rebuild, never emitted by triggers."""
        from denorm.denorms import build_triggerset
        from denorm.models import DirtyInstance

        ts = build_triggerset()
        table = DirtyInstance._meta.db_table
        for name, trigger in ts.triggers.items():
            for action in trigger.actions:
                sql, _ = action.sql()
                if table in sql:
                    assert "func_name" in sql, (
                        f"Trigger {name} inserts a DirtyInstance row "
                        "without func_name — that's a NULL whole-object "
                        "marker; library triggers must emit per-function "
                        "markers only."
                    )

    def test_marker_inserts_use_on_conflict_not_subtransaction(self, db):
        """A plpgsql EXCEPTION block opens a subtransaction on EVERY
        execution — a known Postgres scalability cliff (pg_subtrans SLRU)
        on hot write paths. Bare ON CONFLICT DO NOTHING has identical
        dedup semantics with no subtransaction (spec 2.1)."""
        from denorm.denorms import build_triggerset
        from denorm.models import DirtyInstance

        ts = build_triggerset()
        table = DirtyInstance._meta.db_table
        checked = 0
        for trigger in ts.triggers.values():
            for action in trigger.actions:
                sql, _ = action.sql()
                if table in sql and "INSERT" in sql.upper():
                    checked += 1
                    assert "ON CONFLICT DO NOTHING" in sql
                    assert "EXCEPTION" not in sql.upper()
        assert checked > 0

    def test_update_trigger_conditions_live_in_when_clause(self, db):
        """Postgres evaluates CREATE TRIGGER ... WHEN before invoking the
        trigger function: rows that touch no watched column skip plpgsql
        entirely (spec 2.2). The IF used to live inside the function."""
        from denorm.denorms import build_triggerset

        ts = build_triggerset()
        profile_updates = [
            t
            for t in ts.triggers.values()
            if t.db_table == "test_app_profile" and t.event == "update"
        ]
        assert profile_updates
        for trigger in profile_updates:
            sql, _ = trigger.sql()
            assert "WHEN (" in sql, "UPDATE trigger lost its WHEN clause"
            assert "IS DISTINCT FROM" in sql.split("CREATE TRIGGER")[1], (
                "change-detection must sit in the CREATE TRIGGER WHEN "
                "clause, after the function definition"
            )
            body = sql.split("$$")[1]  # the plpgsql function body
            assert "IF " not in body, (
                "function body still carries the IF — condition must move "
                "to the WHEN clause so non-matching rows never invoke "
                "plpgsql"
            )


class TestMarkDirtyAndNullContract:
    def test_mark_dirty_emits_null_marker_and_flush_full_saves(
        self, transactional_db, denorm_triggers
    ):
        from test_app.models import Profile

        import denorm
        from denorm import denorms
        from denorm.models import DirtyInstance

        p = Profile.objects.create(first_name="John", last_name="Doe")
        denorms.flush()
        DirtyInstance.objects.all().delete()

        # Corrupt both denorm columns without leaving markers behind.
        Profile.objects.filter(pk=p.pk).update(letterhead="BROKEN")
        DirtyInstance.objects.all().delete()

        denorm.mark_dirty(p)
        marker = _markers(Profile, p.pk).get()
        assert marker.func_name is None

        denorms.flush()
        p.refresh_from_db()
        assert p.full_name == "John Doe"
        assert p.letterhead == "Dear John Doe"

    def test_mark_dirty_is_idempotent(self, transactional_db, denorm_triggers):
        from test_app.models import Profile

        import denorm
        from denorm.models import DirtyInstance

        p = Profile.objects.create(first_name="A", last_name="B")
        DirtyInstance.objects.all().delete()
        denorm.mark_dirty(p)
        denorm.mark_dirty(p)  # unique index + ignore_conflicts: no dupe, no error
        assert _markers(Profile, p.pk).count() == 1

    def test_null_takes_precedence_over_field_markers(
        self, transactional_db, denorm_triggers
    ):
        from unittest.mock import patch

        from test_app.models import Profile

        from denorm import denorms
        from denorm.models import DirtyInstance

        p = Profile.objects.create(first_name="John", last_name="Doe")
        denorms.flush()
        DirtyInstance.objects.all().delete()

        ct = ContentType.objects.get_for_model(Profile)
        DirtyInstance.objects.create(
            content_type=ct, object_id=p.pk, func_name="full_name"
        )
        DirtyInstance.objects.create(content_type=ct, object_id=p.pk)  # NULL

        seen = {}
        orig_save = Profile.save

        def spying_save(self, *args, **kwargs):
            seen["kwargs"] = kwargs
            return orig_save(self, *args, **kwargs)

        with patch.object(Profile, "save", spying_save):
            denorms.flush_single(ct.pk, p.pk, ct)

        assert "update_fields" not in seen["kwargs"], (
            "A func_name=NULL marker must force a FULL save (whole-object "
            "recompute), taking precedence over coexisting field markers."
        )

    def test_unknown_func_name_falls_back_to_full_save(
        self, transactional_db, denorm_triggers
    ):
        from test_app.models import Profile

        from denorm import denorms
        from denorm.models import DirtyInstance

        p = Profile.objects.create(first_name="John", last_name="Doe")
        denorms.flush()
        DirtyInstance.objects.all().delete()

        # Corrupt BOTH denorm columns directly so a targeted save would only
        # heal the one it knows about — a full save must heal everything.
        Profile.objects.filter(pk=p.pk).update(letterhead="STALE")
        DirtyInstance.objects.all().delete()

        ct = ContentType.objects.get_for_model(Profile)
        # e.g. a marker from a field that was removed in a later deploy
        DirtyInstance.objects.create(
            content_type=ct, object_id=p.pk, func_name="removed_in_v2"
        )

        denorms.flush_single(ct.pk, p.pk, ct)  # must not raise
        assert not DirtyInstance.objects.exists()

        # Full save must have recomputed BOTH fields, healing the corruption.
        p.refresh_from_db()
        assert p.full_name == "John Doe", (
            "Unknown func_name must trigger a full save that recomputes full_name."
        )
        assert p.letterhead == "Dear John Doe", (
            "Unknown func_name must trigger a full save that heals ALL denorm "
            "columns, including letterhead which was corrupted to 'STALE'."
        )

    def test_mark_dirty_rejects_unsaved_instances(
        self, transactional_db, denorm_triggers
    ):
        from test_app.models import Profile

        import denorm

        with pytest.raises(ValueError):
            denorm.mark_dirty(Profile(first_name="X", last_name="Y"))
