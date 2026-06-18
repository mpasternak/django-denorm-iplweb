from django.conf import settings
from django.db import connection, models

from . import denorms


# Sentinel for the per-save pre_save() cache: ``None`` (and other falsy values)
# are legitimate denorm results, so "absent" must be a value the function can
# never return. getattr(..., _UNSET) distinguishes "not computed yet" from
# "computed and the answer is None".
_UNSET = object()


_SAFE_SELF_FUNCS_CACHE = {}


def _is_plain_column(model, name):
    """True iff ``name`` (a field name or attname) resolves to a concrete
    column of ``model`` that is NOT itself a denormalized field.

    A denormalized field carries a ``denorm`` attribute on its field
    instance (set in ``contribute_to_class``); a plain column does not.
    Unresolvable names are treated as NOT plain (conservative: keep marker).
    """
    by_either_name = {}
    for f in model._meta.concrete_fields:
        by_either_name[f.name] = f
        by_either_name[f.attname] = f
    field = by_either_name.get(name)
    if field is None:
        return False
    return getattr(field, "denorm", None) is None


def _safe_self_funcs(model):
    """Set of denorm field names on ``model`` whose value is a pure function
    of this row's own PLAIN (non-denormalized) columns — and therefore whose
    self-marker is provably redundant after an ORM save that recomputed them.

    A denorm field qualifies iff ALL hold:
      * it is a real denorm field with a callback (``denorm.func``);
      * its dependency list is non-empty AND every dependency is a
        ``DependOnFields`` (no ``@depend_on_related`` / CacheKey / aggregate);
      * every declared dependency name resolves to a plain, non-denorm
        concrete column of the same model.

    Undeclared funcs (empty depend list), chain funcs (a declared name that
    is itself a denorm field), related/aggregate/CacheKey funcs all fail one
    of these and are EXCLUDED — their markers are never dropped here.

    Result is cached per model.
    """
    cached = _SAFE_SELF_FUNCS_CACHE.get(model)
    if cached is not None:
        return cached

    from denorm.dependencies import DependOnFields

    safe = set()
    for f in model._meta.fields:
        denorm = getattr(f, "denorm", None)
        if denorm is None or not getattr(denorm, "func", None):
            continue
        deps = getattr(denorm, "depend", []) or []
        depfields = [d for d in deps if isinstance(d, DependOnFields)]
        # Must have at least one DependOnFields and NO other dependency kind.
        if not depfields or len(depfields) != len(deps):
            continue
        names = {n for d in depfields for n in d.field_names}
        if names and all(_is_plain_column(model, n) for n in names):
            safe.add(denorm.fieldname)

    _SAFE_SELF_FUNCS_CACHE[model] = safe
    return safe


def _drop_redundant_self_markers(sender, instance, update_fields=None, **kwargs):
    """post_save handler: delete this object's provably-redundant self-markers.

    Runs inside the save's transaction, AFTER pre_save recomputed the denorm
    columns and AFTER the self-trigger inserted markers. For each denorm func
    that is a pure function of this row's own plain columns AND was recomputed
    by this save (full save, or func in update_fields), the marker the trigger
    just created is redundant -> delete it. Only for THIS (content_type, pk),
    only func_name IN safe; NULL / chain / related / undeclared / non-recomputed
    markers are never matched.
    """
    safe = _safe_self_funcs(sender)
    if not safe:
        return
    if update_fields is not None:
        safe = safe & set(update_fields)
        if not safe:
            return
    from django.contrib.contenttypes.models import ContentType

    from denorm.models import DirtyInstance

    ct = ContentType.objects.get_for_model(sender)
    DirtyInstance.objects.filter(
        content_type=ct, object_id=instance.pk, func_name__in=safe
    ).delete()


def _clear_denorm_pre_save_cache(sender, instance, **kwargs):
    """Clear cached pre_save values after save completes.

    Django 6.0+ may call Field.pre_save() multiple times per save().
    We cache the result to ensure idempotency, but must clear it after
    save() finishes so that subsequent saves recompute the value.
    See: https://code.djangoproject.com/ticket/36855
    """
    for attr in list(vars(instance)):
        if attr.startswith("_denorm_pre_save_"):
            delattr(instance, attr)


def denormalized(DBField, *args, **kwargs):
    """
    Turns a callable into model field, analogous to python's ``@property`` decorator.
    The callable will be used to compute the value of the field every time the model
    gets saved.
    If the callable has dependency information attached to it the fields value will
    also be recomputed if the dependencies require it.

    **Arguments:**

    DBField (required)
        The type of field you want to use to save the data.
        Note that you have to use the field class and not an instance
        of it.

    \\*args, \\*\\*kwargs:
        Those will be passed unaltered into the constructor of ``DBField``
        once it gets actually created.
    """

    class DenormDBField(DBField):
        """
        Special subclass of the given DBField type, with a few extra additions.
        """

        def __init__(self, func=None, *args, **kwargs):
            self.func = func
            self.skip = kwargs.pop("skip", None)
            self.only = kwargs.pop("only", None)
            kwargs["editable"] = False
            DBField.__init__(self, *args, **kwargs)

        def contribute_to_class(self, cls, name, *args, **kwargs):
            if (
                hasattr(settings, "DENORM_BULK_UNSAFE_TRIGGERS")
                and settings.DENORM_BULK_UNSAFE_TRIGGERS
            ):
                self.denorm = denorms.BaseCallbackDenorm(skip=self.skip, only=self.only)
            else:
                self.denorm = denorms.CallbackDenorm(skip=self.skip, only=self.only)
            self.denorm.func = self.func
            self.denorm.depend = [
                dcls(*dargs, **dkwargs)
                for (dcls, dargs, dkwargs) in getattr(self.func, "depend", [])
            ]
            self.denorm.model = cls
            self.denorm.fieldname = name
            self.field_args = (args, kwargs)
            models.signals.class_prepared.connect(self.denorm.setup, sender=cls)
            # Add The many to many signal for this class
            models.signals.pre_save.connect(denorms.many_to_many_pre_save, sender=cls)
            models.signals.post_save.connect(denorms.many_to_many_post_save, sender=cls)
            models.signals.post_save.connect(
                _clear_denorm_pre_save_cache,
                sender=cls,
                dispatch_uid=f"denorm_clear_pre_save_cache_{cls.__name__}",
            )
            models.signals.post_save.connect(
                _drop_redundant_self_markers,
                sender=cls,
                dispatch_uid=f"denorm_drop_self_markers_{cls.__name__}",
            )
            DBField.contribute_to_class(self, cls, name, *args, **kwargs)

        def pre_save(self, model_instance, add):
            """
            Updates the value of the denormalized field before it gets saved.

            Must be idempotent: Django 6.0+ may call pre_save() more than once
            per save(). We cache the computed value on the model instance to
            avoid re-evaluating the denorm function within the same save cycle.
            See: https://code.djangoproject.com/ticket/36855
            """
            # Cache key unique to this field on this instance for this save cycle.
            cache_attr = f"_denorm_pre_save_{self.attname}"
            cached = getattr(model_instance, cache_attr, _UNSET)
            if cached is not _UNSET:
                return cached

            value = self.denorm.func(model_instance)

            if hasattr(self, "remote_field") and self.remote_field:
                related_field_model = self.remote_field.model
            elif hasattr(self, "related_field"):
                related_field_model = self.related_field.model
            elif hasattr(self, "related"):
                try:
                    related_field_model = self.related.parent_model
                except AttributeError:
                    related_field_model = self.related.model
            else:
                related_field_model = None

            if related_field_model and isinstance(value, related_field_model):
                setattr(model_instance, self.attname, None)
                setattr(model_instance, self.name, value)
                result = getattr(model_instance, self.attname)
            else:
                setattr(model_instance, self.attname, value)
                result = value

            setattr(model_instance, cache_attr, result)
            return result

        def deconstruct(self):
            # Freeze a denormalized field in migrations as its plain DBField:
            # the dynamic DenormDBField subclass has no importable path, so we
            # return the base field's path AND the base field's normalized
            # args/kwargs. Returning super_path together with the subclass's own
            # args/kwargs (the previous behaviour) mixed two sources and could
            # leak denorm-only kwargs into migrations / raise on reconstruction.
            name, path, args, kwargs = super().deconstruct()
            super_name, super_path, super_args, super_kwargs = DBField(
                *args, **kwargs
            ).deconstruct()
            return name, super_path, super_args, super_kwargs

    def deco(func):
        dbfield = DenormDBField(func, *args, **kwargs)
        return dbfield

    return deco


class AggregateField(models.PositiveIntegerField):
    def get_denorm(self, *args, **kwargs):
        """
        Returns denorm instance
        """
        raise NotImplementedError("You need to override this method")

    def __init__(self, manager_name=None, **kwargs):
        """
        **Arguments:**

        manager_name:
            The name of the related manager to be counted.

        filter:
            Filter, which is applied to manager. For example:

        >>> active_item_count = CountField('item_set', filter={'active__exact':True})
        >>> adult_user_count = CountField('user_set', filter={'age__gt':18})

        exclude:
            Do not include filter in aggregation

        Any additional arguments are passed on to the contructor of
        PositiveIntegerField.
        """
        skip = kwargs.pop("skip", None)
        qs_filter = kwargs.pop("filter", {})
        if qs_filter and connection.vendor == "sqlite":
            raise NotImplementedError(
                "filters for aggregate fields are currently not supported for sqlite"
            )
        qs_exclude = kwargs.pop("exclude", {})
        self.denorm = self.get_denorm(skip)
        self.denorm.manager_name = manager_name
        self.denorm.filter = qs_filter
        self.denorm.exclude = qs_exclude
        self.kwargs = kwargs
        kwargs["default"] = 0
        kwargs["editable"] = False
        super().__init__(**kwargs)

    def contribute_to_class(self, cls, name, *args, **kwargs):
        self.denorm.model = cls
        self.denorm.fieldname = name
        models.signals.class_prepared.connect(self.denorm.setup)
        super().contribute_to_class(cls, name, *args, **kwargs)

    def pre_save(self, model_instance, add):
        """Never write an application-side snapshot to this
        trigger-maintained column.

        On INSERT there can be no related rows yet -> 0. On UPDATE return
        `F(column)` so the SQL reads `col = col`: resolved inside the
        UPDATE itself, a concurrent trigger increment between any read
        and this write cannot be lost. The in-memory attribute is NOT
        refreshed by save(); callers needing the current value must
        refresh_from_db().
        """
        if add:
            setattr(model_instance, self.attname, 0)
            return 0
        return models.F(self.attname)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        del kwargs["editable"]
        args = [self.denorm.manager_name] + args
        return name, path, args, kwargs


class CountField(AggregateField):
    """
    A ``PositiveIntegerField`` that stores the number of rows
    related to this model instance through the specified manager.
    The value will be incrementally updated when related objects
    are added and removed.

    """

    def __init__(self, manager_name=None, **kwargs):
        """
        **Arguments:**

        manager_name:
            The name of the related manager to be counted.

        filter:
            Filter, which is applied to manager. For example:

        >>> active_item_count = CountField('item_set', filter={'active__exact':True})
        >>> adult_user_count = CountField('user_set', filter={'age__gt':18})

        Any additional arguments are passed on to the contructor of
        PositiveIntegerField.
        """

        kwargs["editable"] = False
        super().__init__(manager_name, **kwargs)

    def get_denorm(self, skip):
        return denorms.CountDenorm(skip)


class SumField(AggregateField):
    """
    A ``PositiveIntegerField`` that stores sub of related field values
    to this model instance through the specified manager.
    The value will be incrementally updated when related objects
    are added and removed.

    """

    def __init__(self, manager_name=None, field=None, **kwargs):
        self.field = field
        kwargs["editable"] = False
        super().__init__(manager_name, **kwargs)

    def get_denorm(self, skip):
        return denorms.SumDenorm(skip, self.field)


class CacheKeyField(models.BigIntegerField):
    """
    A ``BigIntegerField`` that gets set to a random value anytime
    the model is saved or a dependency is triggered.
    The field gets updated immediately and does not require *denorm.flush()*.
    It currently cannot detect a direct (bulk)update to the model
    it is declared in.
    """

    def __init__(self, **kwargs):
        """
        All arguments are passed on to the contructor of
        BigIntegerField.
        """
        self.dependencies = []
        kwargs["default"] = 0
        kwargs["editable"] = False
        self.kwargs = kwargs
        super().__init__(**kwargs)

    def depend_on_related(self, *args, **kwargs):
        """
        Add dependency information to the CacheKeyField.
        Accepts the same arguments like the *denorm.depend_on_related* decorator
        """
        from .dependencies import CacheKeyDependOnRelated

        self.dependencies.append(CacheKeyDependOnRelated(*args, **kwargs))

    def contribute_to_class(self, cls, name, *args, **kwargs):
        for depend in self.dependencies:
            depend.fieldname = name
        self.denorm = denorms.BaseCacheKeyDenorm(depend_on_related=self.dependencies)
        self.denorm.model = cls
        self.denorm.fieldname = name
        models.signals.class_prepared.connect(self.denorm.setup)
        models.signals.post_save.connect(
            _clear_denorm_pre_save_cache,
            sender=cls,
            dispatch_uid=f"denorm_clear_pre_save_cache_{cls.__name__}",
        )
        super().contribute_to_class(cls, name, *args, **kwargs)

    def pre_save(self, model_instance, add):
        # Must be idempotent: Django 6.0+ may call pre_save() multiple times.
        # See: https://code.djangoproject.com/ticket/36855
        cache_attr = f"_denorm_pre_save_{self.attname}"
        cached = getattr(model_instance, cache_attr, _UNSET)
        if cached is not _UNSET:
            return cached

        value = self.denorm.func(model_instance)
        setattr(model_instance, self.attname, value)
        setattr(model_instance, cache_attr, value)
        return value


class CacheWrapper:
    def __init__(self, field):
        self.field = field

    def __set__(self, obj, value):
        key = "CachedField_%s" % value
        cached = self.field.cache.get(key)
        if not cached:
            cached = self.field.func(obj)
            self.field.cache.set(key, cached, 60 * 60 * 24 * 30)
        obj.__dict__[self.field.name] = cached


class CachedField(CacheKeyField):
    def __init__(self, func=None, cache=None, *args, **kwargs):
        self.func = func
        self.cache = cache
        super().__init__(*args, **kwargs)
        if func and cache:
            for c, a, kw in self.func.depend:
                self.depend_on_related(*a, **kw)

    def contribute_to_class(self, cls, name, *args, **kwargs):
        super().contribute_to_class(cls, name, *args, **kwargs)
        setattr(cls, self.name, CacheWrapper(self))


def cached(cache, *args, **kwargs):
    def deco(func):
        dbfield = CachedField(func, cache, *args, **kwargs)
        return dbfield

    return deco
