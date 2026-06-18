import logging
from itertools import islice

from django.contrib import contenttypes

from .base import get_alldenorms

logger = logging.getLogger(__name__)


def rebuild_instances_of(model, *args, **kwargs):
    # create DirtyInstance for all models

    from denorm.conf import settings
    from denorm.models import DirtyInstance

    content_type = contenttypes.models.ContentType.objects.get_for_model(model)
    objs = (
        DirtyInstance(content_type=content_type, object_id=pk)
        for pk in model.objects.filter(*args, **kwargs).values_list("pk", flat=True)
    )

    while True:
        batch = list(islice(objs, settings.DENORM_BATCH_SIZE))
        if not batch:
            break
        DirtyInstance.objects.bulk_create(
            batch, settings.DENORM_BATCH_SIZE, ignore_conflicts=True
        )


def rebuildall(model_name=None, field_name=None, verbose=False, flush_=True):
    """
    Updates all models containing denormalized fields.
    """

    alldenorms = get_alldenorms()
    models = {}
    for denorm in alldenorms:
        current_app_label = denorm.model._meta.app_label
        current_model_name = denorm.model._meta.model.__name__
        current_app_model = f"{current_app_label}.{current_model_name}"
        if model_name is None or model_name.lower() in (
            current_app_label.lower(),
            current_model_name.lower(),
            current_app_model.lower(),
        ):
            if field_name is None or field_name == denorm.fieldname:
                models.setdefault(denorm.model, []).append(denorm)

    i = 0
    for model, denorms in models.items():
        if verbose:
            for denorm in denorms:
                msg = (
                    "making dirty instances",
                    f"{i + 1}/{len(alldenorms)}",
                    denorm.fieldname,
                    "in",
                    denorm.model,
                )
                logger.info(msg)
                i += 1

        rebuild_instances_of(model)

    if flush_:
        from denorm.denorms import flush

        flush(verbose)


def drop_triggers(using=None):
    from denorm.db import triggers

    triggerset = triggers.TriggerSet(using=using)
    triggerset.drop()


def install_triggers(using=None):
    """
    Installs all required triggers in the database
    """
    build_triggerset(using=using).install()


def build_triggerset(using=None):
    from denorm.db import triggers

    alldenorms = get_alldenorms()

    # Use a TriggerSet to ensure each event gets just one trigger
    triggerset = triggers.TriggerSet(using=using)
    for denorm in alldenorms:
        triggerset.append(denorm.get_triggers(using=using))
    return triggerset
