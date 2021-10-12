# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models

from denorm.db import const


class Migration(migrations.Migration):
    dependencies = [
        ("contenttypes", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="DirtyInstance",
            fields=[
                (
                    "id",
                    models.AutoField(
                        verbose_name="ID",
                        serialize=False,
                        auto_created=True,
                        primary_key=True,
                    ),
                ),
                ("object_id", models.TextField(null=True, blank=True)),
                (
                    "content_type",
                    models.ForeignKey(
                        to="contenttypes.ContentType", on_delete=models.CASCADE
                    ),
                ),
            ],
        ),
        migrations.RunSQL(
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

        CREATE TRIGGER notify_django_denorm_queue
          AFTER INSERT ON denorm_dirtyinstance
          FOR EACH STATEMENT
          EXECUTE PROCEDURE notify_django_denorm_queue();
        """,
            """
            DROP TRIGGER notify_django_denorm_queue ON denorm_dirtyinstance;
            DROP FUNCTION notify_django_denorm_queue;
            """,
        ),
    ]
