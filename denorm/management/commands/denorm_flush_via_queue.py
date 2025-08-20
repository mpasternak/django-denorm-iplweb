from denorms import flush_single
from django.core.management.base import BaseCommand
from models import DirtyInstance


class Command(BaseCommand):
    help = "Recalculates the value of every denormalized field that was marked dirty."

    def handle(self, **kwargs):
        for elem in DirtyInstance.objects.all():
            flush_single.apply_async(kwargs={"pk": elem.pk}, ignore_result=True)
