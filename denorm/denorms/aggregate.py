import abc
import functools

from django.contrib import contenttypes
from django.db import connection, connections
from django.db.models import ManyToManyField, sql
from django.db.models.aggregates import Count, Sum
from django.db.models.query_utils import Q
from django.db.models.sql.compiler import SQLCompiler
from django.db.models.sql.datastructures import Join
from django.db.models.sql.query import Query
from django.db.models.sql.where import WhereNode

try:
    from django.core.exceptions import FullResultSet
except ImportError:

    class FullResultSet(Exception):
        pass


from .base import Denorm

#: PL/pgSQL record variables available inside a row trigger. They look like
#: table aliases to Django's SQL compiler, but they are *not* identifiers that
#: may be quoted -- ``"NEW"."col"`` is read by PostgreSQL as a reference to a
#: table named ``NEW`` and fails with "missing FROM-clause entry for table NEW".
TRIGGER_ALIASES = frozenset(("NEW", "OLD"))


class TriggerAliasQuotingMixin:
    """Never quote the ``NEW`` / ``OLD`` trigger aliases.

    Up to Django 6.0 ``Col.as_sql()`` used ``quote_name_unless_alias()``, which
    left anything registered in ``Query.alias_map`` unquoted -- so the fake
    ``NEW`` / ``OLD`` alias of :class:`TriggerFilterQuery` came out bare by
    accident. Django 6.1 switched ``Col.as_sql()`` to ``quote_name()`` (and
    deprecated ``quote_name_unless_alias()``), which quotes every alias. This
    mixin restores the required behaviour for the two record variables only.
    """

    def quote_name(self, name):
        if name in TRIGGER_ALIASES:
            return name
        if hasattr(SQLCompiler, "quote_name"):  # Django >= 6.1
            return super().quote_name(name)
        return self.connection.ops.quote_name(name)

    def quote_name_unless_alias(self, name):  # Django < 6.1 code paths
        if name in TRIGGER_ALIASES:
            return name
        return super().quote_name_unless_alias(name)


class TriggerSQLCompiler(TriggerAliasQuotingMixin, SQLCompiler):
    """The generic compiler used to compile a filter against ``NEW`` / ``OLD``.

    :class:`TriggerFilterQuery` fakes its single alias, so there is no real
    table to compile against and the backend's own compiler subclass is
    irrelevant here -- the generic one is what this has always used.
    """


@functools.lru_cache(maxsize=None)
def trigger_compiler_class(base):
    """``base`` with the ``NEW`` / ``OLD`` quoting exemption mixed in.

    Used for SQL that a *real* query compiles but that gets spliced into a
    trigger body, where the backend's own compiler subclass must be preserved
    -- so the class is derived from whichever compiler
    ``connection.ops.compiler()`` hands out rather than hardcoded.
    """
    return type(f"Trigger{base.__name__}", (TriggerAliasQuotingMixin, base), {})


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

    @property
    def m2m_field(self):
        """The ``ManyToManyField`` behind ``self.manager``.

        ``manager.field`` is the m2m field itself no matter which side the
        descriptor sits on, so ``m2m_field.model`` is always the model that
        declares the relation — the model whose rows are being aggregated.
        (Pre-1.8 Django exposed the same pair as ``manager.related.field`` /
        ``manager.related.model``; that attribute is long gone.)
        """
        return self.manager.field

    def get_related_subquery(self, using, type, select_field=None, filtered=False):
        """Build the ``(SELECT ... FROM <related table> WHERE ...)`` subquery
        addressing the single related row referenced by the through-table row
        the trigger fired for (``NEW``/``OLD``, per ``type``).

        With ``select_field`` it selects that column; without it, ``COUNT(*)``.

        ``filtered`` applies ``self.filter``/``self.exclude``.  It is off by
        default because only the caller that also propagates the returned
        query parameters may use it — a filter contributes placeholders to
        the SQL, and a caller that drops the params would emit broken SQL.
        """
        qn = self.get_quote_name(using)

        related_query = Query(self.m2m_field.model)
        if filtered:
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
                    qn(self.m2m_field.model._meta.pk.get_attname_column()[1]),
                    type,
                    qn(self.m2m_field.m2m_column_name()),
                )
            ],
            None,
            None,
            None,
        )
        if select_field is None:
            related_query.add_annotation(Count("*"), "__count")
        else:
            related_query.add_fields([select_field])
        related_query.clear_ordering(force=True)
        related_query.default_cols = False
        # This SELECT is spliced into a trigger body, so it is compiled with
        # the NEW/OLD quoting exemption -- see trigger_compiler_class(). The
        # exemption is what keeps the alias safe if a filter ever compiles a
        # Col against it; today's ``NEW.<col>`` comes in through add_extra()
        # as raw SQL, which no compiler touches.
        #
        # ``using`` is None whenever triggers are built for the default
        # connection, so resolve the connection here rather than relying on
        # Query.get_compiler(), which insists on one or the other.
        cconnection = self.get_connection(using)
        compiler_class = trigger_compiler_class(
            cconnection.ops.compiler(related_query.compiler)
        )
        return compiler_class(related_query, cconnection, using).as_sql()

    def get_related_where(self, fk_name, using, type):
        qn = self.get_quote_name(using)

        related_where = [
            "%s = %s.%s"
            % (qn(self.model._meta.pk.get_attname_column()[1]), type, qn(fk_name))
        ]
        related_filter_where, related_where_params = self.get_related_subquery(
            using, type, filtered=True
        )
        if related_filter_where is not None:
            related_where.append("(" + related_filter_where + ") > 0")
        return related_where, related_where_params

    def m2m_triggers(self, content_type, fk_name, related_field, using):
        """
        Returns triggers for m2m relation
        """
        from denorm.db import triggers

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
        from denorm.db import triggers

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
        qn = TriggerSQLCompiler(inc_query, cconnection, using)
        try:
            inc_filter_where, _ = inc_query.where.as_sql(qn, cconnection)
        except FullResultSet:
            inc_filter_where, _ = ("", "")

        dec_query = TriggerFilterQuery(related_model, trigger_alias="OLD")
        dec_query.add_q(Q(**self.filter))
        dec_query.add_q(~Q(**self.exclude))
        qn = TriggerSQLCompiler(dec_query, cconnection, using)
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

    def get_related_value(self, using, type, operator):
        """``<denorm column> +/- (SELECT <summed column> FROM ...)``.

        The subquery reads the summed column off the related row named by the
        through-table row the trigger fired for.  It selects ``self.sum_field``
        (a column of the *related* model) — never ``self.fieldname``, which
        names the denormalized column on ``self.model`` and does not exist
        over there.

        No filter is applied here: the enclosing UPDATE is already gated by
        ``get_related_where()``, so it only runs for rows that pass, and an
        unparameterized value expression cannot carry filter placeholders.
        """
        qn = self.get_quote_name(using)

        related_select, _ = self.get_related_subquery(
            using, type, select_field=self.sum_field
        )
        return f"{qn(self.fieldname)} {operator} ({related_select})"

    def get_related_increment_value(self, using):
        return self.get_related_value(using, "NEW", "+")

    def get_related_decrement_value(self, using):
        return self.get_related_value(using, "OLD", "-")


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
