# -*- coding: utf-8 -*-
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models


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
    object_id = models.TextField(blank=True, null=True)
    content_object = GenericForeignKey()
    func_name = models.TextField(blank=True, null=True)

    def __str__(self):
        ret = f"DirtyInstance: {self.content_type}, {self.object_id}"
        if self.func_name:
            ret += f", {self.func_name}"
        return ret
