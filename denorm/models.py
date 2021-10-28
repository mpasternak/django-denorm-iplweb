# -*- coding: utf-8 -*-
from datetime import timedelta

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils import timezone

DEFAULT_TIMEOUT = timedelta(minutes=5)

WEEK_AGO = timedelta(days=7)


class DirtyInstanceManager(models.Manager):
    def process_next(self):
        return self.filter(processing_started=None).order_by(
            "-created_on", "-func_name"
        )

    def in_processing(self):
        return self.exclude(processing_started=None).filter(processing_finished=None)

    def finished_successfully(self):
        return self.exclude(processing_started=None, processing_finished=None).filter(
            success=True
        )

    def mark_timeouts_as_failed(self, timedelta=None):
        """
        Housekeeping: mark as timed out, default: past 5 minutes
        """
        if timedelta is None:
            timedelta = DEFAULT_TIMEOUT

        now = timezone.now()
        t = now - timedelta

        self.in_processing().filter(processing_started__lte=t).update(
            success=False,
            traceback="Timeout (marked by DirtyInstanceManager.houskeeping)",
            processing_finished=now,
        )

    def everything_older_than(self, timedelta):
        now = timezone.now()
        t = now - timedelta
        return self.filter(created_on__lte=t)

    def housekeeping(self):
        self.mark_timeouts_as_failed()
        self.finished_successfully().delete()
        self.everything_older_than(WEEK_AGO).delete()

    def dump(self):
        def _rows():
            for elem in self.all().order_by("created_on")[:500]:
                yield (
                    elem.pk,
                    elem.content_type,
                    elem.object_id,
                    elem.func_name,
                    elem.created_on,
                    elem.processing_started,
                    elem.processing_finished,
                    elem.success,
                    elem.traceback,
                )

        def _nice():
            import texttable

            table = texttable.Texttable(max_width=160)
            rows = list(_rows())
            table.add_rows(
                [
                    [
                        "pk",
                        "content_type",
                        "object_id",
                        "func_name",
                        "created_on",
                        "processing_started",
                        "processing_finished",
                        "success",
                        "traceback",
                    ]
                ]
                + rows
            )
            print("\n", table.draw(), f"\nTotal: {len(rows)}")

        def _ugly():
            for row in _rows():
                print(row)

        try:
            _nice()
        except ImportError:
            _ugly()


class DirtyInstance(models.Model):
    """
    Holds a reference to a model instance that may contain inconsistent data
    that needs to be recalculated.
    DirtyInstance instances are created by the insert/update/delete triggers
    when related objects change.
    """

    objects = DirtyInstanceManager()

    class Meta:
        app_label = "denorm"

    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    # null=True for object_id is intentional, it is for some weird linked foreign keys
    object_id = models.IntegerField(null=True, blank=True)
    content_object = GenericForeignKey()
    func_name = models.TextField(blank=True, null=True, db_index=True)

    created_on = models.DateTimeField(auto_now_add=True, db_index=True)
    processing_started = models.DateTimeField(null=True, blank=True, db_index=True)
    processing_finished = models.DateTimeField(null=True, blank=True)
    success = models.NullBooleanField(db_index=True)
    traceback = models.TextField(null=True, blank=True)

    def __str__(self):
        ret = f"DirtyInstance: {self.content_type}, {self.object_id}"
        if self.func_name:
            ret += f", func_name={self.func_name}"
        ret += (
            f", created_on={self.created_on}, processing_started={self.processing_started}, "
            f"processing_finished={self.processing_finished}, success={self.success}"
        )
        if self.traceback:
            ret += f", traceback={self.traceback}"
        return ret

    def content_object_for_update(self):
        """Returns a self.content_object, only locked for update. Needs
        to run inside a transaciton. Can return None because nowait=True"""
        klass = self.content_type.model_class()
        return (
            klass.objects.select_for_update(skip_locked=True)
            .filter(pk=self.object_id)
            .first()
        )

    def find_similar(self):
        """Find similar objects to this one. Same content_type, same object_id; func_name if this
        object has func_name, but in case of no func name -- find all objects, as no func name
        means even broader scope:"""
        return DirtyInstance.objects.filter(
            content_type=self.content_type,
            object_id=self.object_id,
        )

    def delete_similar(self):
        """Remove similar DirtyInstances from db, which we haven't yet processed"""
        self.find_similar().filter(
            processing_started=None,
        ).delete()

    def delete_this_and_similar(self):
        self.delete_similar()
        return self.delete()

    def mark_as_being_processed(self):
        self.processing_started = timezone.now()
        self.save()

    def mark_as_processed_successfully(self):
        self.processing_finished = timezone.now()
        self.success = True
        self.save()

    def mark_as_failed(self, tb):
        self.processing_finished = timezone.now()
        self.success = False
        self.traceback = tb
        self.save()
