"""Per-function self-triggers and merged TriggerSet shape."""

from __future__ import annotations

from django.contrib.contenttypes.models import ContentType

from ._helpers import _markers


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
        from unittest.mock import patch

        from test_app.models import Profile

        from denorm import denorms
        from denorm.models import DirtyInstance

        p = Profile.objects.create(first_name="John", last_name="Doe")
        denorms.flush()
        DirtyInstance.objects.all().delete()

        # Corrupt letterhead's column directly (no trigger watches it), then
        # dirty ONLY full_name via its declared source column. We then verify
        # the FIRST save services full_name with a TARGETED update_fields.
        Profile.objects.filter(pk=p.pk).update(letterhead="SENTINEL")
        assert not DirtyInstance.objects.exists()
        Profile.objects.filter(pk=p.pk).update(last_name="Smith")

        ct = ContentType.objects.get_for_model(Profile)

        save_kwargs = []
        orig = Profile.save

        def recording_save(self, *a, **k):
            save_kwargs.append(k.get("update_fields"))
            return orig(self, *a, **k)

        with patch.object(Profile, "save", recording_save):
            denorms.flush_single(ct.pk, p.pk, ct)

        # The FIRST save services the 'full_name' marker and MUST be targeted
        # (update_fields=['full_name']) — targeted markers mean targeted saves,
        # so it does NOT rewrite letterhead on that pass.
        assert save_kwargs[0] == ["full_name"], (
            "first save for a 'full_name' marker must be "
            f"update_fields=['full_name']; got {save_kwargs[0]}"
        )

        # full_name changed (Doe -> Smith), so its column UPDATE cascades a
        # 'letterhead' marker which the convergence loop then recomputes — a
        # SECOND, targeted letterhead save. SENTINEL is correctly overwritten
        # because letterhead's genuine dependency (full_name) changed.
        assert save_kwargs[1] == ["letterhead"], (
            "second save must service the cascaded 'letterhead' marker; "
            f"got {save_kwargs[1]}"
        )
        p.refresh_from_db()
        assert p.full_name == "John Smith"
        assert p.letterhead == "Dear John Smith"

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

        # flush_single changes the full_name COLUMN, whose trigger cascades a
        # 'letterhead' marker for the SAME object. With the convergence loop
        # (spec 2.5) that marker is re-claimed and recomputed IN THE SAME
        # transaction, so the chain settles in one flush_single — no leftover
        # marker for an outer pass.
        denorms.flush_single(ct.pk, p.pk, ct)
        cascade = set(_markers(Profile, p.pk).values_list("func_name", flat=True))
        assert cascade == set(), (
            "convergence loop should settle the full_name -> letterhead chain "
            f"in one flush_single, leaving no markers; got {cascade}"
        )

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

    def test_generic_relation_when_parenthesises_content_type_or(self, db):
        """The content-type element ``(OLD.ct = X) OR (NEW.ct = X)`` must be
        wrapped in its own parens before being AND-joined with the
        field-change conditions.

        ``AND`` binds tighter than ``OR`` in SQL, so the unwrapped form

            ((fields changed)) AND (OLD.ct = X) OR (NEW.ct = X)

        parses as ``((fields changed) AND OLD-ct-match) OR NEW-ct-match`` —
        the trigger fires whenever ``NEW.content_type`` matches even if no
        watched field changed (over-fire). Safe — flush is idempotent — but
        wasteful, and only correct by accident. Wrapping restores the intended
        ``(fields changed) AND (content type relevant, old or new)``.
        """
        from denorm.denorms import build_triggerset

        ts = build_triggerset()
        generic_updates = [
            t
            for t in ts.triggers.values()
            if t.event == "update" and t.content_type_field
        ]
        assert generic_updates, "expected a generic-relation UPDATE trigger"
        for trigger in generic_updates:
            sql, _ = trigger.sql()
            ctf = trigger.content_type_field
            ct = trigger.content_type
            wrapped = '((OLD."%s" = %s) OR (NEW."%s" = %s))' % (ctf, ct, ctf, ct)
            assert wrapped in sql, (
                f"content-type OR group not parenthesised in {trigger.name()}; "
                "AND/OR precedence makes the trigger over-fire on a matching "
                "NEW.content_type even when no watched field changed"
            )
