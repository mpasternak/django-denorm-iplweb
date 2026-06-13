from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models


class DirtyInstance(models.Model):
    """
    Holds a reference to a model instance that may contain inconsistent data
    that needs to be recalculated.
    DirtyInstance instances are created by the insert/update/delete triggers
    when related objects change.

    This is the hottest insert/delete surface in the system, so every extra
    index is pure write amplification and bloat. The intended (and only)
    access path for marker lookups is the expression UNIQUE index created in
    migration 0017 — ``(content_type_id, COALESCE(object_id, -1),
    COALESCE(func_name, ''))``. Claim queries MUST go through
    ``denorm.denorms._markers_for`` so they emit the matching
    ``COALESCE(object_id, -1)`` expression and Postgres can use both leading
    index columns; filtering the raw ``object_id`` column degrades a large
    single-model flush to O(N^2). The FK index on ``content_type_id`` and
    the standalone ``func_name`` / ``created_on`` indexes are intentionally
    absent: the 0017 index's leading column covers FK-cascade scans, and no
    code path filters on the other two columns (spec 2.6).
    """

    class Meta:
        app_label = "denorm"

    content_type = models.ForeignKey(
        ContentType, on_delete=models.CASCADE, db_index=False
    )
    # null=True for object_id is intentional, it is for some weird linked foreign keys
    object_id = models.IntegerField(null=True, blank=True)

    content_object = GenericForeignKey()

    func_name = models.TextField(blank=True, null=True)

    created_on = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        ret = f"DirtyInstance: {self.content_type}, {self.object_id}"
        ret += f", {self.func_name=}"
        ret += f", {self.created_on=}. "
        return ret

    def content_object_for_update(self):
        """Returns a self.content_object, only locked for update. Needs
        to run inside a transaction. Uses skip_locked so contending
        workers don't pile up waiting on the same hot row — they get
        None back and can try a different row."""
        klass = self.content_type.model_class()
        try:
            return klass.objects.select_for_update(skip_locked=True).get(
                pk=self.object_id
            )
        except klass.DoesNotExist:
            return
