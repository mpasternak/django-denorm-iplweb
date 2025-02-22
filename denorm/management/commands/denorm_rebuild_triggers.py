from django.core.management.base import BaseCommand
from django.db import DEFAULT_DB_ALIAS

from denorm import denorms


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument(
            "--database",
            action="store",
            dest="database",
            default=DEFAULT_DB_ALIAS,
            help="Nominates a database to execute "
            'SQL into. Defaults to the "default" database.',
        )

    help = "Recreates all triggers needed by django-denorm."

    def handle(self, **options):
        using = options["database"]
        denorms.drop_triggers(using=using)
        denorms.install_triggers(using=using)
