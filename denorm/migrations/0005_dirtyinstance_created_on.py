# Generated by Django 3.2.7 on 2021-10-03 03:37

import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("denorm", "0004_alter_dirtyinstance_success"),
    ]

    operations = [
        migrations.AddField(
            model_name="dirtyinstance",
            name="created_on",
            field=models.DateTimeField(
                auto_now_add=True, default=django.utils.timezone.now
            ),
            preserve_default=False,
        ),
    ]
