import abc

from django.contrib import contenttypes
from django.db import connection, connections
from django.db.models import ManyToManyField, sql
from django.db.models.aggregates import Sum
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


class TriggerSQLCompiler(SQLCompiler):
    """Compiler that never quotes the ``NEW`` / ``OLD`` trigger aliases.

    Up to Django 6.0 ``Col.as_sql()`` used ``quote_name_unless_alias()``, which
    left anything registered in ``Query.alias_map`` unquoted -- so the fake
    ``NEW`` / ``OLD`` alias of :class:`TriggerFilterQuery` came out bare by
    accident. Django 6.1 switched ``Col.as_sql()`` to ``quote_name()`` (and
    deprecated ``quote_name_unless_alias()``), which quotes every alias. This
    subclass restores the required behaviour for the two record variables only.
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
