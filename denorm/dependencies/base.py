from django.db import connection, connections
from django.db.models.fields import related

from denorm.helpers import find_fks, find_m2ms


def _qv(value):
    # As long as `value` is function.__name__ (a Python identifier) this is
    # safe to inline into trigger SQL. Anything else would be SQL injection.
    return f"'{value}'"


class DenormDependency(object):

    """
    Base class for real dependency classes.
    """

    def get_triggers(self, using):
        """
        Must return a list of ``denorm.triggers.Trigger`` instances
        """
        return []

    def get_quote_name(self, using):
        if using:
            cconnection = connections[using]
        else:
            cconnection = connection
        return cconnection.ops.quote_name

    def setup(self, this_model):
        """
        Remembers the model this dependency was declared in.
        """
        self.this_model = this_model


class DependOnRelated(DenormDependency):
    def __init__(
        self,
        othermodel,
        foreign_key=None,
        type=None,
        skip=None,
        only=None,
        func=None,
    ):
        self.other_model = othermodel
        self.fk_name = foreign_key
        self.type = type
        self.skip = skip or ()
        self.only = only or ()
        self.func = func

    def setup(self, this_model):
        super(DependOnRelated, self).setup(this_model)

        # FIXME: this should not be necessary
        if self.other_model == related.RECURSIVE_RELATIONSHIP_CONSTANT:
            self.other_model = self.this_model

        if isinstance(self.other_model, str):
            # if ``other_model`` is a string, it certainly is a lazy relation.

            def function(local, related, field):
                return self.resolved_model(field, related, local)

            related.lazy_related_operation(
                function, self.this_model, self.other_model, field=None
            )
        else:
            # otherwise it can be resolved directly
            self.resolved_model(None, self.other_model, None)

    def resolved_model(self, data, model, cls):
        """
        Does all the initialization that had to wait until we knew which
        model we depend on.
        """
        self.other_model = model

        # Create a list of all ForeignKeys and ManyToManyFields between both related models, in both directions
        candidates = [
            ("forward", fk)
            for fk in find_fks(self.this_model, self.other_model, self.fk_name)
        ]
        if self.other_model != self.this_model or self.type:
            candidates += [
                ("backward", fk)
                for fk in find_fks(self.other_model, self.this_model, self.fk_name)
            ]
        candidates += [
            ("forward_m2m", fk)
            for fk in find_m2ms(self.this_model, self.other_model, self.fk_name)
        ]
        if self.other_model != self.this_model or self.type:
            candidates += [
                ("backward_m2m", fk)
                for fk in find_m2ms(self.other_model, self.this_model, self.fk_name)
            ]

        # If a relation type was given (forward,backward,forward_m2m or backward_m2m),
        # filter out all relations that do not match this type.
        candidates = [x for x in candidates if not self.type or self.type == x[0]]

        if len(candidates) > 1:
            raise ValueError(
                "%s has more than one ForeignKey or ManyToManyField to %s (or reverse); "
                "cannot auto-resolve. Candidates are: %s\n"
                "HINT: try to specify foreign_key on depend_on_related decorators."
                % (self.this_model, self.other_model, candidates)
            )
        if not candidates:
            raise ValueError(
                "%s has no ForeignKeys or ManyToManyFields to %s (or reverse); cannot auto-resolve."
                % (self.this_model, self.other_model)
            )

        # Now the candidates list contains exactly one item, thats our winner.
        self.type, self.field = candidates[0]
