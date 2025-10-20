from django.core.management.base import BaseCommand
from django.db.models import Count

from denorm.models import DirtyInstance


class Command(BaseCommand):
    help = "Shows the count of dirty instances grouped by content type (model)."

    def handle(self, **kwargs):
        # Query dirty instances grouped by content_type with counts
        counts = (
            DirtyInstance.objects.values("content_type")
            .annotate(count=Count("id"))
            .order_by("-count")
        )

        total = 0

        if not counts:
            self.stdout.write("No dirty instances found.")
            return

        # Display each content type with its count
        for item in counts:
            content_type_id = item["content_type"]
            count = item["count"]
            total += count

            # Get the actual ContentType object to display the model name
            from django.contrib.contenttypes.models import ContentType

            ct = ContentType.objects.get(pk=content_type_id)
            model_name = f"{ct.app_label}.{ct.model}"

            self.stdout.write(f"{model_name}: {count}")

        # Display total
        self.stdout.write("---")
        self.stdout.write(f"Total: {total} dirty instances")
