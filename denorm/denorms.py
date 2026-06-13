import abc
import logging
import random
import sys
import threading
from contextlib import nullcontext
from itertools import islice
from types import SimpleNamespace

from django.apps import apps
from django.contrib import contenttypes
from django.core.exceptions import FieldDoesNotExist

try:
    from django.core.exceptions import FullResultSet
except ImportError:

    class FullResultSet(Exception):
        pass


from django.db import close_old_connections, connection, connections, transaction
from django.db.models import ManyToManyField, sql
from django.db.models.aggregates import Sum
from django.db.models.query_utils import Q
from django.db.models.sql.compiler import SQLCompiler
from django.db.models.sql.datastructures import Join
from django.db.models.sql.query import Query
from django.db.models.sql.where import WhereNode

from denorm.retry import retry_on_serialization_failure

logger = logging.getLogger(__name__)

# Thread-local guard set while flush_single is recomputing an object.
# @denorm_always_dirty consults flush_in_progress() so that flush's OWN
# recompute save() does NOT re-mark the object dirty (which would prevent
# flush from ever converging for an always-dirty model).
_flush_state = threading.local()


def flush_in_progress():
    """True while the current thread is inside flush_single's recompute save."""
    return getattr(_flush_state, "active", False)


def many_to_many_pre_save(sender, instance, **kwargs):
    """
    Updates denormalised many-to-many fields for the model
    """
    if instance.pk:
        # Need a primary key to do m2m stuff
        for m2m in sender._meta.local_many_to_many:
            # This gets us all m2m fields, so limit it to just those that are denormed
            if hasattr(m2m, "denorm"):
                # Does some extra jiggery-pokery for "through" m2m models.
                # May not work under lots of conditions.
                remote = m2m.remote_field
                if hasattr(remote, "through_model"):
                    # Clear exisiting through records (bit heavy handed?)
                    kwargs = {m2m.related.var_name: instance}

                    # Can't use m2m_column_name in a filter
                    # kwargs = { m2m.m2m_column_name(): instance.pk, }
                    remote.through_model.objects.filter(**kwargs).delete()

                    values = m2m.denorm.func(instance)
                    for value in values:
                        kwargs.update({m2m.m2m_reverse_name(): value.pk})
                        remote.through_model.objects.create(**kwargs)

                else:
                    values = m2m.denorm.func(instance)
                    getattr(instance, m2m.attname).set(values)


def many_to_many_post_save(sender, instance, created, **kwargs):
    if created:

        def check_resave():
            for m2m in sender._meta.local_many_to_many:
                if hasattr(m2m, "denorm"):
                    return True
            return False

        if check_resave():
            instance.save()


def get_alldenorms():
    """
    Get all denormalizations.
    """
    alldenorms = []
    for model in apps.get_models(include_auto_created=True):
        if not model._meta.proxy:
            for field in model._meta.fields:
                if hasattr(field, "denorm"):
                    if not field.denorm.model._meta.swapped:
                        alldenorms.append(field.denorm)
    return alldenorms


class Denorm:
    def __init__(self, skip=None, only=None):
        self.func = None
        self.skip = skip
        self.only = only

    def get_quote_name(self, using):
        if using:
            cconnection = connections[using]
        else:
            cconnection = connection
        return cconnection.ops.quote_name

    def setup(self, **kwargs):
        """
        Adds 'self' to the global denorm list
        and connects all needed signals.
        """

    def get_triggers(self, using):
        return []


class BaseCallbackDenorm(Denorm):
    """
    Handles the denormalization of one field, using a python function
    as a callback.
    """

    def setup(self, **kwargs):
        """
        Calls setup() on all DenormDependency resolvers
        """
        super().setup(**kwargs)

        for dependency in self.depend:
            dependency.setup(self.model)

    def get_triggers(self, using):
        """
        Creates a list of all triggers needed to keep track of changes
        to fields this denorm depends on.
        """
        trigger_list = list()

        # Get the triggers of all DenormDependency instances attached
        # to our callback.
        for dependency in self.depend:
            trigger_list += dependency.get_triggers(using=using)
        ret = trigger_list + super().get_triggers(using=using)
        return ret


class CallbackDenorm(BaseCallbackDenorm):
    """
    As above, but with extra self-triggers as described below.
    """

    def get_triggers(self, using):
        qn = self.get_quote_name(using)

        content_type = str(
            contenttypes.models.ContentType.objects.get_for_model(self.model).pk
        )

        # Self-triggers exist because a row may change without the ORM
        # running pre_save (bulk update, raw SQL). They insert PER-FUNCTION
        # markers (content_type, object_id, '<func name>'); the library
        # never emits func_name=NULL itself — NULL is reserved for explicit
        # whole-object marking (denorm.mark_dirty, rebuild_instances_of)
        # and takes precedence in flush_single.
        from .db import triggers
        from .dependencies import DependOnFields, _qv
        from .models import DirtyInstance

        action = triggers.TriggerActionInsert(
            model=DirtyInstance,
            columns=("content_type_id", "object_id", "func_name"),
            values=(
                content_type,
                "NEW.%s" % qn(self.model._meta.pk.get_attname_column()[1]),
                # _qv: func.__name__ is a Python identifier — safe to inline.
                _qv(self.func.__name__),
            ),
        )

        trigger_list = [
            # Unconditional INSERT marker for every function (OLD/NEW
            # comparison is impossible on insert; covers raw-SQL inserts).
            # NO func argument: all functions of a model share the trigger
            # name, so TriggerSet merges their actions into one trigger —
            # safe, because INSERT triggers carry no change-conditions.
            triggers.Trigger(
                self.model,
                "after",
                "insert",
                [action],
                content_type,
                using,
                self.skip,
                self.only,
            ),
        ]

        if not any(isinstance(d, DependOnFields) for d in self.depend):
            # Undeclared function: conservative UPDATE trigger — fires when
            # any watched column EXCEPT this function's own changes (same
            # conditions as the old catch-all minus the own column),
            # addressed to this function. Excluding the own column stops
            # non-deterministic functions from re-marking themselves forever
            # (flush writes the column -> trigger would re-fire); undeclared
            # chains keep working because OTHER functions' columns stay
            # watched. Consequence: bulk-writing a denormalized column
            # directly is unsupported (it no longer self-heals).
            # Declared functions get their targeted UPDATE trigger from
            # DependOnFields via super().get_triggers().
            # skip/watch-lists are attname-based (a denormalized FK field
            # "forum" stores in column "forum_id"), hence get_field().attname.
            own_attname = self.model._meta.get_field(self.fieldname).attname
            # The trigger name must NOT collide with the function's
            # depend_on_related('self') triggers (same table, same event,
            # same func suffix): TriggerSet.append merges same-named
            # triggers and keeps the FIRST watch-list — our own-column
            # exclusion would then silence the dependency's cascade
            # actions (e.g. tree path / recursive count propagation).
            # A distinct "_self" suffix keeps them separate.
            self_trigger_name = SimpleNamespace(
                __name__=self.func.__name__ + "_self",
                __qualname__=self.func.__qualname__ + "_self",
            )
            trigger_list.append(
                triggers.Trigger(
                    self.model,
                    "after",
                    "update",
                    [action],
                    content_type,
                    using,
                    tuple(self.skip or ()) + (own_attname,),
                    self.only,
                    self_trigger_name,
                )
            )

        return trigger_list + super().get_triggers(using=using)


class BaseCacheKeyDenorm(Denorm):
    def __init__(self, depend_on_related, *args, **kwargs):
        self.depend = depend_on_related
        super().__init__(*args, **kwargs)
        import random

        self.func = lambda o: random.randint(-9223372036854775808, 9223372036854775807)

    def setup(self, **kwargs):
        """
        Calls setup() on all DenormDependency resolvers
        """
        super().setup(**kwargs)

        for dependency in self.depend:
            dependency.setup(self.model)

    def get_triggers(self, using):
        """
        Creates a list of all triggers needed to keep track of changes
        to fields this denorm depends on.
        """
        trigger_list = list()

        # Get the triggers of all DenormDependency instances attached
        # to our callback.
        for dependency in self.depend:
            trigger_list += dependency.get_triggers(using=using)

        return trigger_list + super().get_triggers(using=using)


class CacheKeyDenorm(BaseCacheKeyDenorm):
    """
    As above, but with extra triggers on self as described below
    """

    def get_triggers(self, using):
        qn = self.get_quote_name(using)

        content_type = str(
            contenttypes.models.ContentType.objects.get_for_model(self.model).pk
        )

        # This is only really needed if the instance was changed without
        # using the ORM or if it was part of a bulk update.
        # In those cases the self_save_handler won't get called by the
        # pre_save signal
        from .db import triggers

        action = triggers.TriggerActionUpdate(
            model=self.model,
            columns=(self.fieldname,),
            values=(triggers.RandomBigInt(),),
            where="%s = NEW.%s"
            % ((qn(self.model._meta.pk.get_attname_column()[1]),) * 2),
        )
        trigger_list = [
            triggers.Trigger(
                self.model,
                "after",
                "update",
                [action],
                content_type,
                using,
                self.skip,
                self.only,
            ),
            triggers.Trigger(
                self.model,
                "after",
                "insert",
                [action],
                content_type,
                using,
                self.skip,
                self.only,
            ),
        ]

        return trigger_list + super().get_triggers(using=using)


class TriggerWhereNode(WhereNode):
    def sql_for_columns(self, data, qn, connection, internal_type=None):
        """
        Returns the SQL fragment used for the left-hand side of a column
        constraint (for example, the "T1.foo" portion in the clause
        "WHERE ... T1.foo = 6").
        """
        table_alias, name, db_type = data
        if table_alias:
            if table_alias in ("NEW", "OLD"):
                lhs = f"{table_alias}.{qn(name)}"
            else:
                lhs = f"{qn(table_alias)}.{qn(name)}"
        else:
            lhs = qn(name)
        try:
            response = connection.ops.field_cast_sql(db_type, internal_type) % lhs
        except TypeError:
            response = connection.ops.field_cast_sql(db_type) % lhs
        return response


class TriggerFilterQuery(sql.Query):
    def __init__(self, model, trigger_alias, where=TriggerWhereNode):
        super().__init__(model, where)
        self.trigger_alias = trigger_alias

        class JoinField:
            def get_joining_columns(self):
                return None

            def get_joining_fields(self):
                return ()

        join = Join(None, None, None, None, JoinField(), False)
        self.alias_map = {trigger_alias: join}

    def get_initial_alias(self):
        return self.trigger_alias


class AggregateDenorm(Denorm):
    __metaclass__ = abc.ABCMeta

    def __init__(self, skip=None, only=None):
        self.manager = None
        self.skip = skip
        self.only = only

    def setup(self, sender, **kwargs):
        # as we connected to the ``class_prepared`` signal for any sender
        # and we only need to setup once, check if the sender is our model.
        if sender is self.model:
            super().setup(sender=sender, **kwargs)

        # related managers will only be available after both models are initialized
        # so check if its available already, and get our manager
        if not self.manager and hasattr(self.model, str(self.manager_name)):
            self.manager = getattr(self.model, self.manager_name)

    def get_related_where(self, fk_name, using, type):
        qn = self.get_quote_name(using)

        related_where = [
            "%s = %s.%s"
            % (qn(self.model._meta.pk.get_attname_column()[1]), type, qn(fk_name))
        ]
        related_query = Query(self.manager.related.model)
        for name, value in self.filter.items():
            related_query.add_q(Q(**{name: value}))
        for name, value in self.exclude.items():
            related_query.add_q(~Q(**{name: value}))
        related_query.add_extra(
            None,
            None,
            [
                "%s = %s.%s"
                % (
                    qn(self.model._meta.pk.get_attname_column()[1]),
                    type,
                    qn(self.manager.related.field.m2m_column_name()),
                )
            ],
            None,
            None,
            None,
        )
        related_query.add_count_column()
        related_query.clear_ordering(force_empty=True)
        related_query.default_cols = False
        related_filter_where, related_where_params = related_query.get_compiler(
            using=using
        ).as_sql()
        if related_filter_where is not None:
            related_where.append("(" + related_filter_where + ") > 0")
        return related_where, related_where_params

    def m2m_triggers(self, content_type, fk_name, related_field, using):
        """
        Returns triggers for m2m relation
        """
        from .db import triggers

        related_inc_where, _ = self.get_related_where(fk_name, using, "NEW")
        related_dec_where, related_where_params = self.get_related_where(
            fk_name, using, "OLD"
        )
        related_increment = triggers.TriggerActionUpdate(
            model=self.model,
            columns=(self.fieldname,),
            values=(self.get_related_increment_value(using),),
            where=(" AND ".join(related_inc_where), related_where_params),
        )
        related_decrement = triggers.TriggerActionUpdate(
            model=self.model,
            columns=(self.fieldname,),
            values=(self.get_related_decrement_value(using),),
            where=(" AND ".join(related_dec_where), related_where_params),
        )
        trigger_list = [
            triggers.Trigger(
                related_field,
                "after",
                "update",
                [related_increment, related_decrement],
                content_type,
                using,
                self.skip,
            ),
            triggers.Trigger(
                related_field,
                "after",
                "insert",
                [related_increment],
                content_type,
                using,
                self.skip,
            ),
            triggers.Trigger(
                related_field,
                "after",
                "delete",
                [related_decrement],
                content_type,
                using,
                self.skip,
            ),
        ]
        return trigger_list

    def get_triggers(self, using):
        from .db import triggers

        if using:
            cconnection = connections[using]
        else:
            cconnection = connection

        qn = self.get_quote_name(using)

        related_field = self.manager.field
        if isinstance(related_field, ManyToManyField):
            fk_name = related_field.m2m_reverse_name()
            inc_where = [
                "%(id)s IN (SELECT %(reverse_related)s FROM %(m2m_table)s WHERE %(related)s = NEW.%(id)s)"
                % {
                    "id": qn(self.model._meta.pk.get_attname_column()[0]),
                    "related": qn(related_field.m2m_column_name()),
                    "m2m_table": qn(related_field.m2m_db_table()),
                    "reverse_related": qn(fk_name),
                }
            ]
            dec_where = [action.replace("NEW.", "OLD.") for action in inc_where]
        else:
            pk_name = qn(self.model._meta.pk.get_attname_column()[1])
            fk_name = qn(related_field.attname)
            inc_where = [f"{pk_name} = NEW.{fk_name}"]
            dec_where = [f"{pk_name} = OLD.{fk_name}"]

        content_type = str(
            contenttypes.models.ContentType.objects.get_for_model(self.model).pk
        )

        related_model = self.manager.field.model
        inc_query = TriggerFilterQuery(related_model, trigger_alias="NEW")
        inc_query.add_q(Q(**self.filter))
        inc_query.add_q(~Q(**self.exclude))
        qn = SQLCompiler(inc_query, cconnection, using)
        try:
            inc_filter_where, _ = inc_query.where.as_sql(qn, cconnection)
        except FullResultSet:
            inc_filter_where, _ = ("", "")

        dec_query = TriggerFilterQuery(related_model, trigger_alias="OLD")
        dec_query.add_q(Q(**self.filter))
        dec_query.add_q(~Q(**self.exclude))
        qn = SQLCompiler(dec_query, cconnection, using)
        try:
            dec_filter_where, where_params = dec_query.where.as_sql(qn, cconnection)
        except FullResultSet:
            dec_filter_where, where_params = ("", "")

        if inc_filter_where:
            inc_where.append(inc_filter_where)
        if dec_filter_where:
            dec_where.append(dec_filter_where)
            # create the triggers for the incremental updates
        increment = triggers.TriggerActionUpdate(
            model=self.model,
            columns=(self.fieldname,),
            values=(self.get_increment_value(using),),
            where=(" AND ".join(inc_where), where_params),
        )
        decrement = triggers.TriggerActionUpdate(
            model=self.model,
            columns=(self.fieldname,),
            values=(self.get_decrement_value(using),),
            where=(" AND ".join(dec_where), where_params),
        )

        trigger_list = [
            triggers.Trigger(
                related_model,
                "after",
                "update",
                [increment, decrement],
                content_type,
                using,
                self.skip,
            ),
            triggers.Trigger(
                related_model,
                "after",
                "insert",
                [increment],
                content_type,
                using,
                self.skip,
            ),
            triggers.Trigger(
                related_model,
                "after",
                "delete",
                [decrement],
                content_type,
                using,
                self.skip,
            ),
        ]
        if isinstance(related_field, ManyToManyField):
            trigger_list.extend(
                self.m2m_triggers(content_type, fk_name, related_field, using)
            )
        return trigger_list

    @abc.abstractmethod
    def get_increment_value(self, using):
        """
        Returns SQL for incrementing value
        """

    @abc.abstractmethod
    def get_decrement_value(self, using):
        """
        Returns SQL for decrementing value
        """


class SumDenorm(AggregateDenorm):
    """
    Handles denormalization of a sum field by doing incrementally updates.
    """

    def __init__(self, skip=None, field=None):
        super().__init__(skip)
        # in case we want to set the value without relying on the
        # correctness of the incremental updates we create a function that
        # calculates it from scratch.
        self.sum_field = field
        self.func = lambda obj: (
            getattr(obj, self.manager_name)
            .filter(**self.filter)
            .exclude(**self.exclude)
            .aggregate(Sum(self.sum_field))
            .values()[0]
            or 0
        )

    def get_increment_value(self, using):
        qn = self.get_quote_name(using)

        return f"{qn(self.fieldname)} + NEW.{qn(self.sum_field)}"

    def get_decrement_value(self, using):
        qn = self.get_quote_name(using)

        return f"{qn(self.fieldname)} - OLD.{qn(self.sum_field)}"

    def get_related_increment_value(self, using):
        qn = self.get_quote_name(using)

        related_query = Query(self.manager.related.model)
        related_query.add_extra(
            None,
            None,
            [
                "%s = %s.%s"
                % (
                    qn(self.model._meta.pk.get_attname_column()[1]),
                    "NEW",
                    qn(self.manager.related.field.m2m_column_name()),
                )
            ],
            None,
            None,
            None,
        )
        related_query.add_fields([self.fieldname])
        related_query.clear_ordering(force_empty=True)
        related_query.default_cols = False
        related_filter_where, related_where_params = related_query.get_compiler(
            using=using
        ).as_sql()
        return f"{qn(self.fieldname)} + ({related_filter_where})"

    def get_related_decrement_value(self, using):
        qn = self.get_quote_name(using)

        related_query = Query(self.manager.related.model)
        related_query.add_extra(
            None,
            None,
            [
                "%s = %s.%s"
                % (
                    qn(self.model._meta.pk.get_attname_column()[1]),
                    "OLD",
                    qn(self.manager.related.field.m2m_column_name()),
                )
            ],
            None,
            None,
            None,
        )
        related_query.add_fields([self.fieldname])
        related_query.clear_ordering(force_empty=True)
        related_query.default_cols = False
        related_filter_where, related_where_params = related_query.get_compiler(
            using=using
        ).as_sql()
        return f"{qn(self.fieldname)} - ({related_filter_where})"


class CountDenorm(AggregateDenorm):
    """
    Handles the denormalization of a count field by doing incrementally
    updates.
    """

    def __init__(self, skip=None, only=None):
        super().__init__(skip=skip, only=only)
        # in case we want to set the value without relying on the
        # correctness of the incremental updates we create a function that
        # calculates it from scratch.
        self.func = (
            lambda obj: getattr(obj, self.manager_name)
            .filter(**self.filter)
            .exclude(**self.exclude)
            .count()
        )

    def get_increment_value(self, using):
        qn = self.get_quote_name(using)

        return "%s + 1" % qn(self.fieldname)

    def get_decrement_value(self, using):
        qn = self.get_quote_name(using)

        return "%s - 1" % qn(self.fieldname)

    def get_related_increment_value(self, using):
        return self.get_increment_value(using)

    def get_related_decrement_value(self, using):
        return self.get_decrement_value(using)


def rebuild_instances_of(model, *args, **kwargs):
    # create DirtyInstance for all models

    from denorm.conf import settings

    from .models import DirtyInstance

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


def mark_dirty(*instances):
    """Explicitly mark whole objects dirty.

    Creates func_name=NULL markers — the only NULL markers the library
    produces besides rebuild_instances_of(). NULL means "recompute every
    denormalized field of this object" and takes precedence over
    field-level markers in flush_single.
    """
    if any(instance.pk is None for instance in instances):
        raise ValueError("mark_dirty() requires saved instances (pk is None).")

    from django.contrib.contenttypes.models import ContentType

    from .models import DirtyInstance

    markers = [
        DirtyInstance(
            content_type=ContentType.objects.get_for_model(instance),
            object_id=instance.pk,
        )
        for instance in instances
    ]
    DirtyInstance.objects.bulk_create(markers, ignore_conflicts=True)


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
        flush(verbose)


def drop_triggers(using=None):
    from .db import triggers

    triggerset = triggers.TriggerSet(using=using)
    triggerset.drop()


def install_triggers(using=None):
    """
    Installs all required triggers in the database
    """
    build_triggerset(using=using).install()


def build_triggerset(using=None):
    from .db import triggers

    alldenorms = get_alldenorms()

    # Use a TriggerSet to ensure each event gets just one trigger
    triggerset = triggers.TriggerSet(using=using)
    for denorm in alldenorms:
        triggerset.append(denorm.get_triggers(using=using))
    return triggerset


INTERACTIVE = False


class _DirtyInstanceFlushProgress:
    def __init__(self, stream=None, interval_range=(1.0, 3.0)):
        self.stream = stream or sys.stderr
        self.interval_range = interval_range
        self._bar = None
        self._thread = None
        self._stop = threading.Event()

    def __enter__(self):
        from tqdm import tqdm

        self._bar = tqdm(
            total=0,
            desc="denorm_dirtyinstance",
            unit="row",
            file=self.stream,
            leave=True,
            bar_format="{desc}: {total_fmt} left [{elapsed}]",
        )
        self._refresh()
        self._thread = threading.Thread(
            target=self._poll,
            name="denorm-flush-progress",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(self.interval_range) + 0.5)
        self._refresh()
        if self._bar is not None:
            self._bar.close()
        close_old_connections()
        return False

    def _poll(self):
        close_old_connections()
        try:
            while not self._stop.wait(random.uniform(*self.interval_range)):
                self._refresh()
        finally:
            close_old_connections()

    def _refresh(self):
        from .models import DirtyInstance

        close_old_connections()
        try:
            remaining = DirtyInstance.objects.count()
        except Exception:
            logger.exception("denorm flush progress monitor failed")
            self._stop.set()
            return

        self._set_remaining(remaining)

    def _set_remaining(self, remaining):
        if self._bar is None:
            return

        self._bar.n = 0
        self._bar.total = remaining
        self._bar.refresh()


def _markers_for(content_type_id, object_id):
    """All markers for the logical pair, filtered so the 0017 expression
    index is fully usable.

    The unique index keys on COALESCE(object_id, -1); filtering the raw
    column would fall back to scanning the whole content-type prefix —
    O(backlog) per object, O(backlog^2) per flush.
    """
    from django.db.models import Value
    from django.db.models.functions import Coalesce

    from .models import DirtyInstance

    return DirtyInstance.objects.alias(
        _oid=Coalesce("object_id", Value(-1))
    ).filter(
        content_type_id=content_type_id,
        _oid=-1 if object_id is None else object_id,
    )


def _claim_and_delete_markers(content_type_id, object_id):
    """Lock, snapshot and DELETE all claimable markers for the pair.

    Returns the set of claimed ``func_name`` values (an empty set when
    nothing is claimable; ``None`` membership means a whole-object marker).
    Deleting at claim time (inside the caller's transaction) frees the
    unique-index key, so a colliding marker INSERT — from this transaction's
    own save() triggers or from a concurrent writer — waits for our commit
    instead of being silently dropped by the unique_violation handler.
    See docs/spec-concurrency-performance-fixes.md item 1.5.
    """
    from .models import DirtyInstance

    locked_pks = list(
        _markers_for(content_type_id, object_id)
        .select_for_update(skip_locked=True)
        .values_list("pk", flat=True)
    )
    if not locked_pks:
        return set()
    func_names = set(
        DirtyInstance.objects.filter(pk__in=locked_pks).values_list(
            "func_name", flat=True
        )
    )
    DirtyInstance.objects.filter(pk__in=locked_pks).delete()
    return func_names


def _build_save_kwargs(
    obj, func_names, disable_autotime_during_flush, autotime_field_names
):
    """Translate claimed marker func_names into save() keyword arguments.

    NULL (None) in ``func_names`` -> full save (no update_fields); otherwise
    ``update_fields`` of the validated denorm field names, minus any auto_now
    field exclusions when autotime suppression is enabled during flush.
    """
    kw = {}
    if None not in func_names:
        update_fields = []
        for func_name in func_names:
            try:
                obj._meta.get_field(func_name)
                update_fields.append(func_name)
            except FieldDoesNotExist:
                continue
        if update_fields:
            kw["update_fields"] = update_fields

    if disable_autotime_during_flush and autotime_field_names:
        # Build an explicit update_fields that EXCLUDES auto_now fields,
        # so save() doesn't touch them. This replaces the old
        # suppress_autotime() approach which mutated class-level
        # Field.auto_now and leaked across threads.
        if "update_fields" in kw:
            kw["update_fields"] = [
                f for f in kw["update_fields"] if f not in autotime_field_names
            ]
        else:
            kw["update_fields"] = [
                f.name
                for f in obj._meta.local_fields
                if not f.primary_key and f.name not in autotime_field_names
            ]

    return kw


@retry_on_serialization_failure
def flush_single(content_type_id, object_id, content_type=None):
    from denorm.conf import settings

    disable_autotime_during_flush = settings.DENORM_DISABLE_AUTOTIME_DURING_FLUSH
    autotime_field_names = settings.DENORM_AUTOTIME_FIELD_NAMES

    if content_type is None:
        from django.contrib.contenttypes.models import ContentType

        content_type = ContentType.objects.get_for_id(content_type_id)

    with transaction.atomic():
        klass = content_type.model_class()

        # Lock the object row before claiming markers; every acquisition
        # uses skip_locked, so flush workers never wait on each other and
        # this ordering cannot deadlock flush-vs-flush.
        try:
            obj = klass.objects.select_for_update(of=("self",), skip_locked=True).get(
                pk=object_id
            )
        except klass.DoesNotExist:
            # Either the row is locked by another worker, or it has been
            # deleted between marker creation and this lookup. Distinguish:
            # if the row truly doesn't exist, we own the cleanup; otherwise
            # leave the markers untouched for whoever holds the lock.
            if klass.objects.filter(pk=object_id).exists():
                return  # locked elsewhere; another worker will handle it
            _claim_and_delete_markers(content_type.pk, object_id)
            return

        # Suppress flush-internal NOTIFY: the statement-level trigger on
        # denorm_dirtyinstance (migration 0019) skips pg_notify when
        # denorm.flushing = 'on'. SET LOCAL is transaction-scoped (auto-reset
        # at commit/rollback) and applies to THIS connection, so the marker
        # INSERTs fired by obj.save() below — same transaction, same
        # connection — see it and do NOT wake the queue for work this flush is
        # already doing. Genuine writes on other connections are unaffected.
        # Set here (after the object lock, before any save) so every path that
        # calls obj.save() has it; the early-return paths above never save.
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL denorm.flushing = 'on'")

        # Claim AND DELETE the markers now, before save(): while a claimed
        # marker still exists, the unique index silently swallows identical
        # marker inserts (our own save's triggers, concurrent writers),
        # losing invalidations.
        func_names = _claim_and_delete_markers(content_type.pk, object_id)
        if not func_names:
            return

        # Convergence loop (audit spec 2.5): a save() that changes a stored
        # value fires triggers that insert NEW markers for this same object
        # (a same-model denorm chain, e.g. full_name -> letterhead). Those
        # markers are created in OUR transaction and are visible to a re-claim
        # before commit (the same MVCC fact spec 1.5 relies on), so we process
        # them here instead of leaving them for another outer flush() pass.
        #
        # Reusing the locked, in-memory `obj` across iterations is safe: we
        # hold select_for_update(of=('self',)) on the row for the whole
        # transaction, so no concurrent write can change this object's own
        # source columns, and the own-column trigger exclusion means our own
        # saves never re-mark themselves. The re-claimed markers are therefore
        # genuine downstream chain links (recomputed from in-memory denorm
        # fields prior pre_save already refreshed) or dependency-driven markers
        # (recomputed via a fresh query) — never stale in-memory source data.
        #
        # Scope discipline: the re-claim filters to THIS (ct, oid) only;
        # cascade markers for OTHER objects are left for the normal flush path.
        #
        # Guard window: while we call obj.save() to recompute, set the
        # flush-in-progress flag so @denorm_always_dirty's post_save handler
        # does NOT re-mark this object (otherwise flush would never converge
        # for an always-dirty model). Save/restore the previous value so the
        # retry decorator's re-invocation, nested calls, and the early-return
        # paths above (which never reach here) all behave correctly.
        prev_flush_active = getattr(_flush_state, "active", False)
        _flush_state.active = True
        try:
            for passes_left in range(settings.DENORM_MAX_CONVERGE_PASSES, 0, -1):
                obj.save(
                    **_build_save_kwargs(
                        obj,
                        func_names,
                        disable_autotime_during_flush,
                        autotime_field_names,
                    )
                )
                if passes_left == 1:
                    # Cap reached: do NOT re-claim. Any markers our last save
                    # inserted (non-deterministic denorm functions, or a chain
                    # deeper than the cap) stay in the table and are handled by
                    # flush()'s outer loop, bounded by DENORM_MAX_FLUSH_PASSES.
                    break
                func_names = _claim_and_delete_markers(content_type.pk, object_id)
                if not func_names:
                    break
        finally:
            _flush_state.active = prev_flush_active


def flush(
    run_once=False,
    *,
    progress=False,
    progress_stream=None,
    progress_interval=(1.0, 3.0),
):
    """
    Updates all model instances marked as dirty by the DirtyInstance
    model.
    After this method finishes the DirtyInstance table is empty and
    all denormalized fields have consistent data.

    If progress is true, a tqdm-style counter periodically queries the
    DirtyInstance table and displays the current number of rows left.
    """

    # Loop until break.
    # We may need multiple passes, because an update on one instance
    # may cause an other instance to be marked dirty (dependency chains)

    # Get all dirty markers

    ran_once = False

    from denorm.conf import settings

    from .models import DirtyInstance

    progress_context = (
        _DirtyInstanceFlushProgress(
            stream=progress_stream,
            interval_range=progress_interval,
        )
        if progress
        else nullcontext()
    )

    with progress_context:
        passes = 0
        while True:
            if run_once and ran_once:
                break

            if passes >= settings.DENORM_MAX_FLUSH_PASSES:
                remaining = list(
                    DirtyInstance.objects.values_list(
                        "content_type_id", flat=True
                    ).distinct()
                )
                if not remaining:
                    # Converged exactly on the final allowed pass.
                    return
                from django.contrib.contenttypes.models import ContentType

                remaining_labels = sorted(
                    f"{ct.app_label}.{ct.model}"
                    for ct in ContentType.objects.filter(pk__in=remaining)
                )
                logger.error(
                    "denorm.flush: aborting after %d passes; still-dirty "
                    "models=%s. A denormalized function is likely "
                    "non-deterministic (returns a different value on every "
                    "recompute), so flushing can never converge.",
                    passes,
                    remaining_labels,
                )
                return
            passes += 1

            processed = 0
            for content_type_id, object_id in (
                DirtyInstance.objects.all()
                .values_list("content_type_id", "object_id")
                .distinct()
                .iterator(chunk_size=2000)
            ):
                flush_single(content_type_id, object_id)
                processed += 1

            if not processed:
                return

            ran_once = True
