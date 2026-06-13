from django.db import migrations

from denorm.db import const


class Migration(migrations.Migration):
    """Suppress flush-internal NOTIFY (audit #2, Part B).

    The statement-level NOTIFY trigger on denorm_dirtyinstance fires on every
    marker INSERT, including the markers a flush's own recompute saves insert.
    Guard pg_notify behind a session GUC: flush_single sets
    ``SET LOCAL denorm.flushing = 'on'`` inside its transaction, so its marker
    INSERTs no longer wake the queue for work it is already doing. Genuine
    writes (GUC unset -> current_setting returns NULL -> IS DISTINCT FROM 'on')
    still NOTIFY normally.

    The queue path self-converges via a chord (Part A), so it no longer depends
    on flush-internal NOTIFY to rediscover cross-object cascades.

    Reverse restores the unconditional NOTIFY from migration 0016.
    """

    dependencies = [
        ("denorm", "0018_alter_dirtyinstance_content_type_and_more"),
    ]

    operations = [
        migrations.RunSQL(
            # Forward: NOTIFY only when not flushing.
            f"""
    -- Notify when a record is inserted into the denorm dirty table, UNLESS
    -- we are inside a flush (denorm.flushing = 'on', set via SET LOCAL).
    CREATE OR REPLACE FUNCTION notify_django_denorm_queue()
      RETURNS trigger AS $$
    DECLARE
    BEGIN
      IF current_setting('denorm.flushing', true) IS DISTINCT FROM 'on' THEN
        PERFORM pg_notify('{const.DENORM_QUEUE_NAME}', '');
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
            """,
            # Reverse: the unconditional version from migration 0016.
            f"""
    -- Notify when record get inserted into 'django_denorm' table
    CREATE OR REPLACE FUNCTION notify_django_denorm_queue()
      RETURNS trigger AS $$
    DECLARE
    BEGIN
      PERFORM pg_notify('{const.DENORM_QUEUE_NAME}', '');
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
            """,
        )
    ]
