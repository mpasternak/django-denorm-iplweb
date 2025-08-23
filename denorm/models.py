from datetime import timedelta

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models

DEFAULT_TIMEOUT = timedelta(minutes=5)

WEEK_AGO = timedelta(days=7)


class DirtyInstance(models.Model):
    """
    Holds a reference to a model instance that may contain inconsistent data
    that needs to be recalculated.
    DirtyInstance instances are created by the insert/update/delete triggers
    when related objects change.
    """

    class Meta:
        app_label = "denorm"

    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    # null=True for object_id is intentional, it is for some weird linked foreign keys
    object_id = models.IntegerField(null=True, blank=True)

    content_object = GenericForeignKey()

    func_name = models.TextField(blank=True, null=True, db_index=True)

    created_on = models.DateTimeField(auto_now_add=True, db_index=True)

    def __str__(self):
        ret = f"DirtyInstance: {self.content_type}, {self.object_id}"
        ret += f", {self.func_name=}"
        ret += f", {self.created_on=}. "
        return ret

    def content_object_for_update(self):
        """Returns a self.content_object, only locked for update. Needs
        to run inside a transaciton."""
        klass = self.content_type.model_class()
        try:
            return klass.objects.select_for_update().get(pk=self.object_id)
        except klass.DoesNotExist:
            return

    def find_similar(self, **kwargs):
        """Find similar objects to this one. Same content_type, same object_id; func_name if this
        object has func_name, but in case of no func name -- find all objects, as no func name
        means even broader scope:"""
        return DirtyInstance.objects.filter(
            content_type=self.content_type, object_id=self.object_id, **kwargs
        )

    def delete_similar(self):
        """Remove similar DirtyInstances from db, which we haven't yet processed"""
        self.find_similar().select_for_update(skip_locked=True).delete()

    def delete_this_and_similar(self):
        self.delete_similar()
        return self.delete()
