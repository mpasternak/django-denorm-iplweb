"""The PL/pgSQL record aliases NEW / OLD must never be quoted.

``@denormalized`` aggregates with a ``filter``/``exclude`` compile that filter
with Django's own SQL compiler, using a fake ``NEW`` / ``OLD`` table alias
(``denorm.denorms.aggregate.TriggerFilterQuery``). Inside a row trigger those
are PL/pgSQL record variables, not identifiers: PostgreSQL reads ``"NEW"."age"``
as a column of a table named ``NEW`` and rejects the statement with
``missing FROM-clause entry for table "NEW"``.

Django 6.1 changed ``Col.as_sql()`` from ``quote_name_unless_alias()`` to
``quote_name()``, so aliases are now quoted unconditionally — which broke every
filtered aggregate. ``TriggerSQLCompiler`` exempts the two record variables.
"""

from __future__ import annotations

import pytest


@pytest.mark.django_db
def test_filtered_aggregate_triggers_do_not_quote_new_old():
    from denorm.denorms import build_triggerset

    all_sql = "\n".join(t.sql()[0] for t in build_triggerset().triggers.values())

    # The filtered aggregates in test_app put NEW/OLD-qualified columns into
    # the trigger WHERE clause; they must stay bare record references.
    assert 'NEW."age"' in all_sql
    assert 'OLD."age"' in all_sql

    assert '"NEW".' not in all_sql
    assert '"OLD".' not in all_sql
