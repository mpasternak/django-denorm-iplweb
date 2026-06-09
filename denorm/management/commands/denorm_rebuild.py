from django.core.management.base import BaseCommand

from denorm import denorms


def _raw_stream(stream):
    return getattr(stream, "_out", stream)


def _stream_isatty(stream):
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


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
        run_flush = not no_flush

        denorms.rebuildall(
            verbose=verbosity > 1,
            model_name=model_name,
            flush_=False,
        )

        if not run_flush:
            return

        progress_stream = _raw_stream(self.stderr)
        denorms.flush(
            run_once=verbosity > 1,
            progress=_stream_isatty(progress_stream),
            progress_stream=progress_stream,
        )
