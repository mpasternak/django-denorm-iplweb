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
                # Deduplicate existing rows in a single pass so the unique
                # index can be built. The previous approach was a self-join
                # (DELETE ... USING ... IS NOT DISTINCT FROM) which was
                # effectively O(n^2): IS NOT DISTINCT FROM cannot drive a
                # hash/merge join, so Postgres fell back to a nested loop,
                # and every duplicate group of size k materialised
                # k*(k-1)/2 row pairs — catastrophic on exactly the runaway
                # DirtyInstance growth this index exists to fix. row_number()
                # does the same job in one sort (O(n log n)), keeping the
                # newest row (highest id) per
                # (content_type_id, COALESCE(object_id, -1),
                #  COALESCE(func_name, '')) group.
                "DELETE FROM denorm_dirtyinstance "
                "WHERE id IN ("
                "    SELECT id FROM ("
                "        SELECT id, row_number() OVER ("
                "            PARTITION BY content_type_id, "
                "                         COALESCE(object_id, -1), "
                "                         COALESCE(func_name, '') "
                "            ORDER BY id DESC"
                "        ) AS rn"
                "        FROM denorm_dirtyinstance"
                "    ) d"
                "    WHERE d.rn > 1"
                "); "
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
