from types import SimpleNamespace

from django.contrib import contenttypes

from denorm.helpers import content_type_select_sql

from .base import Denorm


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
        # Resolved at trigger-fire time so a renumbered content-type table
        # (e.g. TransactionTestCase teardown) can't strand the marker insert
        # against a stale id — see helpers.content_type_select_sql.
        content_type_value = content_type_select_sql(self.model)

        # Self-triggers exist because a row may change without the ORM
        # running pre_save (bulk update, raw SQL). They insert PER-FUNCTION
        # markers (content_type, object_id, '<func name>'); the library
        # never emits func_name=NULL itself — NULL is reserved for explicit
        # whole-object marking (denorm.mark_dirty, rebuild_instances_of)
        # and takes precedence in flush_single.
        from denorm.db import triggers
        from denorm.dependencies import DependOnFields, _qv
        from denorm.models import DirtyInstance

        action = triggers.TriggerActionInsert(
            model=DirtyInstance,
            columns=("content_type_id", "object_id", "func_name"),
            values=(
                content_type_value,
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
        from denorm.db import triggers

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
