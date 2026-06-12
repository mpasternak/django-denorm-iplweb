"""AST-based audit of @depend_on_fields declarations.

Checks:
  denorm.E001  declared function reads an undeclared sibling column
  denorm.E002  declared name is not a concrete field / self-dependency
  denorm.W001  undeclared function reads sibling columns (nudge)
  denorm.W002  declared function uses dynamic access; cannot fully verify

The scanner is a linter: sound on what it reports, incomplete on what it
cannot see (dynamic access, properties, deep helper indirection).
Silence per-check via SILENCED_SYSTEM_CHECKS.
"""

import ast
import inspect
import textwrap

from django.core import checks


class _SelfReadVisitor(ast.NodeVisitor):
    def __init__(self, selfname):
        self.selfname = selfname
        self.reads = set()
        self.called_methods = set()
        self.uncertain = False

    def visit_Attribute(self, node):
        if isinstance(node.value, ast.Name) and node.value.id == self.selfname:
            self.reads.add(node.attr)
        self.generic_visit(node)

    def visit_Call(self, node):
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == self.selfname
        ):
            self.called_methods.add(func.attr)
        elif isinstance(func, ast.Name) and func.id == "getattr":
            if (
                node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == self.selfname
            ):
                self.uncertain = True
        self.generic_visit(node)


def scan_callable(func):
    """Return (reads, called_methods, uncertain) for self.<attr> accesses."""
    try:
        source = textwrap.dedent(inspect.getsource(func))
    except (OSError, TypeError):
        return set(), set(), True
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set(), set(), True

    fdef = next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ),
        None,
    )
    if fdef is None or not fdef.args.args:
        return set(), set(), True

    visitor = _SelfReadVisitor(fdef.args.args[0].arg)
    visitor.visit(fdef)
    return visitor.reads, visitor.called_methods, visitor.uncertain


def _normalize_to_attnames(model, names):
    """Map field names/attnames to attnames; unknown names pass through."""
    mapping = {}
    for f in model._meta.concrete_fields:
        mapping[f.name] = f.attname
        mapping[f.attname] = f.attname
    return {mapping.get(n, n) for n in names}


def audit_denorm(d):
    """Yield CheckMessages for one callback denorm (model/fieldname/func/depend)."""
    from denorm.dependencies import DependOnFields

    model = d.model
    fieldname = d.fieldname
    func = d.func
    obj = f"{model.__name__}.{fieldname}"

    field_attnames = {f.attname for f in model._meta.concrete_fields}
    known_names = field_attnames | {f.name for f in model._meta.concrete_fields}
    own = _normalize_to_attnames(model, {fieldname})

    deps = [x for x in getattr(d, "depend", []) if isinstance(x, DependOnFields)]
    declared_raw = set()
    for dep in deps:
        declared_raw |= set(dep.field_names)

    # E002 — bad declared names / self-dependency.
    for name in sorted(declared_raw):
        if name == fieldname or _normalize_to_attnames(model, {name}) <= own:
            yield checks.Error(
                f"@depend_on_fields of {obj} declares its own field {name!r} "
                f"(self-dependency).",
                obj=obj,
                id="denorm.E002",
            )
        elif name not in known_names:
            yield checks.Error(
                f"@depend_on_fields of {obj} declares {name!r}, which is not "
                f"a concrete field of {model.__name__}. Available: "
                f"{', '.join(sorted(field_attnames))}.",
                obj=obj,
                id="denorm.E002",
            )

    reads, called_methods, uncertain = scan_callable(func)

    # Follow one level of self._helper() calls into same-class functions.
    for method_name in sorted(called_methods):
        target = getattr(model, method_name, None)
        target = inspect.unwrap(target) if target is not None else None
        if inspect.isfunction(target):
            more_reads, more_calls, more_uncertain = scan_callable(target)
            reads |= more_reads
            uncertain = uncertain or more_uncertain or bool(more_calls)
        else:
            uncertain = True

    # pk reads are identity guards, not data dependencies — never report them.
    pk_attname = {model._meta.pk.attname}
    sibling_reads = _normalize_to_attnames(
        model, {r for r in reads if r in known_names}
    ) - own - pk_attname

    if deps:
        declared = _normalize_to_attnames(
            model, {n for n in declared_raw if n in known_names}
        )
        undeclared_reads = sorted(sibling_reads - declared)
        if undeclared_reads:
            yield checks.Error(
                f"{obj} reads sibling column(s) "
                f"{', '.join(undeclared_reads)} not listed in its "
                f"@depend_on_fields declaration — changes to them will NOT "
                f"mark this field dirty (silent staleness).",
                obj=obj,
                id="denorm.E001",
            )
        if uncertain:
            yield checks.Warning(
                f"{obj} declares @depend_on_fields but uses dynamic attribute "
                f"access or calls the scanner cannot follow; declarations "
                f"cannot be fully verified.",
                obj=obj,
                id="denorm.W002",
            )
    elif sibling_reads:
        yield checks.Warning(
            f"{obj} reads sibling column(s) {', '.join(sorted(sibling_reads))} "
            f"without @depend_on_fields. It is covered by the conservative "
            f"any-column trigger; declare the dependencies to get precise "
            f"invalidation.",
            obj=obj,
            id="denorm.W001",
        )


@checks.register(checks.Tags.models)
def check_depend_on_fields(app_configs, **kwargs):
    from denorm.denorms import BaseCallbackDenorm, get_alldenorms

    messages = []
    for d in get_alldenorms():
        if isinstance(d, BaseCallbackDenorm) and getattr(d, "func", None):
            messages.extend(audit_denorm(d))
    return messages
