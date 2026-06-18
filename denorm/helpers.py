# -*- coding: utf-8 -*-
from django.db import models


def content_type_select_sql(model):
    """
    Return an SQL scalar subquery that resolves ``model``'s
    ``django_content_type`` id *at trigger-fire time*, e.g.::

        (SELECT id FROM django_content_type
          WHERE app_label = 'forum' AND model = 'post')

    Triggers insert this into ``denorm_dirtyinstance.content_type_id``
    instead of an integer literal baked at trigger-build time. A baked
    literal is only correct while the content-type table is never
    renumbered after the triggers were installed. That assumption breaks:

    * under Django's ``TransactionTestCase`` teardown, which TRUNCATEs
      ``django_content_type`` and lets the ``post_migrate`` handler
      recreate the rows with fresh, drifting ids (the common case — every
      ``transaction=True`` test can shift the ids out from under the
      triggers), and
    * more rarely in production after a restored dump that renumbered the
      table, or after ``manage.py remove_stale_contenttypes`` followed by
      recreation, without a ``denorm.install_triggers`` rebuild.

    When the literal goes stale the marker insert references a content
    type row that no longer exists and the FK
    ``denorm_dirtyinstance_content_type_id_...`` raises ForeignKeyViolation.
    Resolving the id dynamically removes the assumption entirely. The
    lookup is a single probe of the ``(app_label, model)`` unique index on
    a tiny, fully cached table, so the per-fire cost is negligible.

    The (app_label, model_name) pair is taken from the *concrete* model —
    exactly what ``ContentType.objects.get_for_model`` stores — so proxy
    models resolve to the same content type the ORM would use.
    """
    from django.contrib.contenttypes.models import ContentType

    opts = model._meta.concrete_model._meta

    def _quote(value):
        return "'%s'" % str(value).replace("'", "''")

    return (
        "(SELECT id FROM %(table)s "
        "WHERE app_label = %(app)s AND model = %(model)s)"
        % {
            "table": ContentType._meta.db_table,
            "app": _quote(opts.app_label),
            "model": _quote(opts.model_name),
        }
    )


def remote_field_model(field):
    if hasattr(field, 'remote_field') and field.remote_field:  # in Django>=1.9
        remote_field_model = field.remote_field.model
        if remote_field_model == 'self':
            return field.model
        return remote_field_model
    if hasattr(field, 'rel') and field.rel:
        return field.rel.to

def find_fks(from_model, to_model, fk_name=None):
    """
    Finds all ForeignKeys on 'from_model' pointing to 'to_model'.
    If 'fk_name' is given only ForeignKeys matching that name are returned.
    """
    # get all ForeignKeys
    fkeys = [x for x in from_model._meta.fields if isinstance(x, models.ForeignKey)]

    # filter out all FKs not pointing to 'to_model'. Compare the resolved model
    # classes by identity, not by repr() string: repr-comparison was lowercased
    # (so two models differing only in case would collapse) and stringly-typed
    # for no benefit — distinct classes already compare unequal.
    fkeys = [x for x in fkeys if remote_field_model(x) == to_model]

    # if 'fk_name' was given, filter out all FKs not matching that name, leaving
    # only one (or none)
    if fk_name:
        fk_name = fk_name if isinstance(fk_name, str) else fk_name.attname
        fkeys = [x for x in fkeys if x.attname in (fk_name, fk_name + '_id')]

    return fkeys


def find_m2ms(from_model, to_model, m2m_name=None):
    """
    Finds all ManyToManyFields on 'from_model' pointing to 'to_model'.
    If 'm2m_name' is given only ManyToManyFields matching that name are returned.
    """
    # get all ManyToManyFields
    try:
        private_fields = from_model._meta.private_fields
    except:  # Django<2.0
        private_fields = from_model._meta.virtual_fields
    m2ms = list(from_model._meta.many_to_many) + private_fields

    # filter out all M2Ms not pointing to 'to_model' (identity, not repr — see
    # find_fks). remote_field_model() returns None for fields with no relation
    # (e.g. a GenericForeignKey in private_fields); None != to_model drops them,
    # which is the intended behaviour.
    m2ms = [x for x in m2ms if remote_field_model(x) == to_model]

    # if 'm2m_name' was given, filter out all M2Ms not matching that name, leaving
    # only one (or none)
    if m2m_name:
        m2m_name = m2m_name if isinstance(m2m_name, str) else m2m_name.attname
        m2ms = [x for x in m2ms if x.attname == m2m_name]

    return m2ms
