"""Regression: denorm triggers must resolve content-type ids dynamically.

Baking the ``django_content_type`` id as an integer literal at
trigger-build time strands every ``denorm_dirtyinstance`` marker insert the
moment the content-type table is renumbered — most visibly under Django's
``TransactionTestCase`` teardown, which TRUNCATEs the table and lets the
``post_migrate`` handler recreate the rows with fresh, drifting ids. The
marker insert then references a content type that no longer exists and the
FK ``denorm_dirtyinstance_content_type_id_...`` raises ForeignKeyViolation.

Every trigger that writes a DirtyInstance marker must therefore emit a
``SELECT id FROM django_content_type WHERE app_label = ... AND model = ...``
subquery, resolved at fire time, instead of a baked integer literal.
"""

import re

import pytest


def _marker_insert_lines(sql):
    """Lines of a trigger body that INSERT a DirtyInstance marker.

    Each action renders on a single line (see ``db.triggers``), so the whole
    ``INSERT INTO denorm_dirtyinstance (...) VALUES/SELECT ...`` — content
    type expression included — lives on one line we can assert against.
    """
    return [
        line
        for line in sql.splitlines()
        if "denorm_dirtyinstance" in line and "content_type_id" in line
    ]


def _collect_marker_inserts():
    from denorm import denorms

    triggerset = denorms.build_triggerset()
    lines = []
    for trigger in triggerset.triggers.values():
        sql, _ = trigger.sql()
        lines.extend(_marker_insert_lines(sql))
    return lines


@pytest.mark.django_db
def test_dirtyinstance_markers_resolve_content_type_dynamically():
    insert_lines = _collect_marker_inserts()

    assert insert_lines, (
        "expected the test project to define at least one trigger that writes "
        "a denorm_dirtyinstance marker"
    )

    for line in insert_lines:
        assert "SELECT id FROM django_content_type" in line, (
            "DirtyInstance marker insert must resolve content_type_id with a "
            "runtime subquery, but a content-type value was baked at "
            f"trigger-build time:\n{line}"
        )
        assert "app_label =" in line and "model =" in line, (
            "the content-type subquery must look the row up by "
            f"(app_label, model):\n{line}"
        )


@pytest.mark.django_db
def test_no_bare_integer_content_type_literal_in_markers():
    """The content_type_id value must never be a bare integer literal.

    Catches a regression where some — but not all — marker inserts were
    converted to the dynamic subquery. We look at the value rendered for the
    leading ``content_type_id`` column: it must be a ``(SELECT ...)`` scalar
    subquery, never ``VALUES (6, ...)`` / ``SELECT DISTINCT 6, ...``.
    """
    baked_literal = re.compile(
        r"(?:VALUES\s*\(|SELECT\s+DISTINCT\s+)\s*\d",
        re.IGNORECASE,
    )
    for line in _collect_marker_inserts():
        assert not baked_literal.search(line), (
            "a denorm_dirtyinstance marker still bakes an integer content "
            f"type into its first inserted value:\n{line}"
        )
