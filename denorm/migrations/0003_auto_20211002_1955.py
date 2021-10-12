# Generated by Django 3.2.7 on 2021-10-02 19:55

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("denorm", "0002_dirtyinstance_func_name"),
    ]

    operations = [
        migrations.AddField(
            model_name="dirtyinstance",
            name="processing_finished",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="dirtyinstance",
            name="processing_started",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="dirtyinstance",
            name="success",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="dirtyinstance",
            name="traceback",
            field=models.TextField(blank=True, null=True),
        ),
    ]
