"""Tests for audit #3: merged triggers must UNION their watched-column lists.

Background
----------
``denorm.db.base.TriggerSet.append`` keys triggers by ``trigger.name()``
(table + time + event + content_type + func.__qualname__). When two source
triggers collide on that name, the set merges the newcomer's *actions* into
the existing trigger but originally KEPT only the existing trigger's
``self.fields`` (the watched-column list that becomes the CREATE TRIGGER
``WHEN`` clause), DISCARDING the newcomer's watch-list.

Consequence of the old behaviour: a change to a column that lived only in the
discarded watch-list would NOT fire the trigger -> missed invalidation ->
STALE DATA (a silent under-fire).

Fix: on a name collision, UNION the newcomer's ``fields`` into the existing
trigger. Because colliding triggers share the same func, their actions insert
the same ``(content_type, object_id, func)`` marker; unioning the watch-lists
turns a dangerous under-fire into a harmless over-fire (the same marker is
inserted on a few more column changes; flush is idempotent).
"""

from __future__ import annotations


def _named_func(name):
    """Stand-in for a @denormalized callback with a fixed name/qualname.

    Both __name__ and __qualname__ matter: PG ``Trigger.name()`` appends
    ``func.__qualname__`` to the trigger name, so two triggers collide on a
    name only when they share the same func qualname (among table/time/event/
    content_type). Pinning both keeps the two triggers we build below on the
    SAME name, exercising the merge path.
    """

    def func(self):
        return ""

    func.__name__ = name
    func.__qualname__ = name
    return func


def _build_two_colliding_triggers():
    """Two PG triggers, same table/time/event/content_type/func, but DIFFERENT
    ``only=`` watch-lists drawn from two real columns of test_app.Member."""
    from test_app.models import Member

    from denorm.db import triggers

    func = _named_func("watchlist_merge_probe")
    content_type = "1"

    action_a = triggers.TriggerActionInsert(
        model=Member,
        columns=("first_name",),
        values=("NEW.first_name",),
    )
    action_b = triggers.TriggerActionInsert(
        model=Member,
        columns=("name",),
        values=("NEW.name",),
    )

    trig_a = triggers.Trigger(
        Member,
        "after",
        "update",
        [action_a],
        content_type,
        None,
        None,
        ("first_name",),  # only -> watch only first_name
        func,
    )
    trig_b = triggers.Trigger(
        Member,
        "after",
        "update",
        [action_b],
        content_type,
        None,
        None,
        ("name",),  # only -> watch only name
        func,
    )
    return trig_a, trig_b


class TestTriggerSetMergeUnionsWatchlist:
    def test_merged_trigger_unions_fields_and_actions(self, db):
        """Unit: appending two colliding triggers with different watch-lists
        must yield a merged trigger that watches BOTH columns and carries
        BOTH source actions.

        Pre-fix this FAILS: only the first trigger's field (first_name) is
        kept and the second's (name) is silently dropped.
        """
        from denorm.db import triggers

        trig_a, trig_b = _build_two_colliding_triggers()
        assert trig_a.name() == trig_b.name(), "test setup: names must collide"

        ts = triggers.TriggerSet(using=None)
        ts.append([trig_a, trig_b])

        # Exactly one merged trigger under the shared name.
        assert len(ts.triggers) == 1
        merged = ts.triggers[trig_a.name()]

        watched = {f[0] for f in merged.fields}
        assert "first_name" in watched, "existing trigger's column must remain"
        assert "name" in watched, (
            "newcomer's column was DROPPED -> under-fire / stale data "
            "(the bug this fix addresses)"
        )

        # Both source actions are present (action merge already worked pre-fix).
        assert len(merged.actions) == 2

    def test_merged_trigger_preserves_field_order_and_dedups(self, db):
        """Existing fields keep their order; a column present in both source
        watch-lists is not duplicated."""
        from test_app.models import Member

        from denorm.db import triggers

        func = _named_func("watchlist_merge_dedup_probe")
        ct = "1"
        act = triggers.TriggerActionInsert(
            model=Member, columns=("name",), values=("NEW.name",)
        )

        trig_a = triggers.Trigger(
            Member, "after", "update", [act], ct, None, None,
            ("first_name", "name"), func,
        )
        trig_b = triggers.Trigger(
            Member, "after", "update", [act], ct, None, None,
            ("name",), func,  # overlaps with trig_a
        )

        ts = triggers.TriggerSet(using=None)
        ts.append([trig_a, trig_b])
        merged = ts.triggers[trig_a.name()]
        watched = [f[0] for f in merged.fields]

        # existing order preserved, no dup of "name"
        assert watched.count("name") == 1
        assert watched.index("first_name") < watched.index("name")


class TestMergedTriggerSQL:
    def test_when_clause_references_both_columns(self, db):
        """Generated SQL: the merged UPDATE trigger's WHEN clause must
        reference BOTH watched columns with ``IS DISTINCT FROM``, OR-joined.
        """
        from denorm.db import triggers

        trig_a, trig_b = _build_two_colliding_triggers()
        ts = triggers.TriggerSet(using=None)
        ts.append([trig_a, trig_b])
        merged = ts.triggers[trig_a.name()]

        sql, _params = merged.sql()

        assert "WHEN (" in sql
        assert '"first_name" IS DISTINCT FROM' in sql, (
            "first watched column missing from WHEN clause"
        )
        assert '"name" IS DISTINCT FROM' in sql, (
            "second watched column missing from WHEN clause -> trigger would "
            "not fire on that column changing"
        )
        # The two per-column conditions are OR-joined inside the WHEN.
        when_fragment = sql.split("WHEN (")[1].split("EXECUTE")[0]
        assert " OR " in when_fragment
