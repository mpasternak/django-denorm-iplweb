# Generated by Django 3.2.7 on 2021-10-03 04:07

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("denorm", "0007_auto_created_on_now"),
    ]

    operations = [
        migrations.AlterField(
            model_name="dirtyinstance",
            name="object_id",
            field=models.IntegerField(blank=True, null=True),
        ),
    ]
