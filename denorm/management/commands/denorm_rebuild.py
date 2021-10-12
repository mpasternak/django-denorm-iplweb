from django.core.management.base import BaseCommand

from denorm import denorms


class Command(BaseCommand):
    help = "Recalculates the value of every single denormalized model field in the whole project."

    def add_arguments(self, parser):
        parser.add_argument(
            "--no-flush",
            action="store_true",
            help="Specify this if you alredy run denorm_queue in background",
        )
        parser.add_argument("--model-name", type=str, default=None)

    def handle(self, no_flush, model_name, *args, **kwargs):
        verbosity = int((kwargs.get("verbosity", 0)))
        denorms.rebuildall(
            verbose=verbosity > 1,
            model_name=model_name,
            flush_=not no_flush,
        )
