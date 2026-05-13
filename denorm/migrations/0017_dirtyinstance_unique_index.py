from django.db import migrations


class Migration(migrations.Migration):
    """Add a UNIQUE index on (content_type_id, object_id, COALESCE(func_name, ''))
    so that:

    * The ``EXCEPTION WHEN unique_violation`` handler that ``TriggerActionInsert``
      wraps every dirty-marker INSERT in actually fires, deduplicating
      runaway DirtyInstance growth from chained triggers.
    * ``rebuild_instances_of`` and any other ``bulk_create`` that passes
      ``ignore_conflicts=True`` can safely be run concurrently.

    The functional ``COALESCE`` is necessary because ``func_name`` is nullable
    and Postgres treats every NULL as distinct under a plain UNIQUE constraint.
    """

    dependencies = [
        ("denorm", "0016_not_parametrized_notify"),
    ]

    operations = [
        migrations.RunSQL(
            sql=(
                # First, deduplicate any existing rows so the index can be built.
                "DELETE FROM denorm_dirtyinstance a USING denorm_dirtyinstance b "
                "WHERE a.id < b.id "
                "AND a.content_type_id = b.content_type_id "
                "AND a.object_id IS NOT DISTINCT FROM b.object_id "
                "AND a.func_name IS NOT DISTINCT FROM b.func_name; "
                # COALESCE on both nullable columns so NULL is treated as
                # a single value under the unique index (Postgres treats
                # NULL as distinct otherwise — `null=True` on object_id
                # for "weird linked foreign keys" would let dupes through).
                "CREATE UNIQUE INDEX IF NOT EXISTS denorm_dirtyinstance_unique "
                "ON denorm_dirtyinstance "
                "(content_type_id, COALESCE(object_id, -1), COALESCE(func_name, ''));"
            ),
            reverse_sql=("DROP INDEX IF EXISTS denorm_dirtyinstance_unique;"),
        ),
    ]
