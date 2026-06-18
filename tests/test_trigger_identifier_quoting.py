"""Generated trigger SQL must quote every identifier it splices in.

Audit finding #2: ``db_table``, WHERE-clause column keys and INSERT/UPDATE
target columns were interpolated raw, while the value side next to them was
already wrapped in ``qn()``. A model with a custom ``db_table`` or a
denormalized field whose name is a PostgreSQL reserved word (``order``,
``user`` ...) therefore produced broken trigger SQL — or a trigger that
silently failed to write its DirtyInstance marker, leaving denormalized data
stale.

These tests exercise the ``.sql()`` generators directly; they need Django
configured (for ``connection.ops`` and a model's ``_meta``) but touch no
database.
"""

from __future__ import annotations

from django.db import connection

from denorm.db.triggers import (
    TriggerActionInsert,
    TriggerActionUpdate,
    TriggerNestedSelect,
)
from denorm.models import DirtyInstance

qn = connection.ops.quote_name


def test_nested_select_quotes_table_and_where_keys():
    sel = TriggerNestedSelect(
        "some weird table",
        ("id",),
        **{"order": "NEW.author_id"},
    )
    sql, params = sel.sql()

    assert qn("some weird table") in sql  # FROM "some weird table"
    assert f'{qn("order")} = NEW.author_id' in sql  # WHERE "order" = ...


def test_nested_select_does_not_mangle_expression_columns():
    # The SELECT list legitimately holds SQL expressions (subqueries, quoted
    # literals), not bare identifiers — they must pass through untouched.
    sel = TriggerNestedSelect(
        "t",
        ("(SELECT id FROM django_content_type)", "'func_name'"),
        **{"fk_id": "NEW.id"},
    )
    sql, _ = sel.sql()

    assert "(SELECT id FROM django_content_type)" in sql
    assert "'func_name'" in sql


def test_action_insert_quotes_table_and_columns():
    ins = TriggerActionInsert(
        model=DirtyInstance,
        columns=("order", "object_id"),
        values=("1", "2"),
    )
    sql, _ = ins.sql()

    assert qn(DirtyInstance._meta.db_table) in sql
    assert f'({qn("order")}, {qn("object_id")})' in sql


def test_action_update_quotes_table_and_target_columns():
    upd = TriggerActionUpdate(
        model=DirtyInstance,
        columns=("order",),
        values=("5",),
        where="1 = 1",
    )
    sql, _ = upd.sql()

    assert qn(DirtyInstance._meta.db_table) in sql
    assert f'{qn("order")} = 5' in sql


def test_quoting_is_idempotent_for_prequoted_identifiers():
    # quote_name is idempotent, so a caller that already quoted an identifier
    # must not end up double-quoted.
    ins = TriggerActionInsert(
        model=DirtyInstance,
        columns=(qn("object_id"),),
        values=("1",),
    )
    sql, _ = ins.sql()

    assert '""' not in sql  # no doubled quotes anywhere
    assert qn("object_id") in sql
