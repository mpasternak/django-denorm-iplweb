# Design: `@depend_on_fields` — declarative same-model dependencies

Status: approved (design review 2026-06-12)
Target version: 1.12.0
Related: `docs/spec-concurrency-performance-fixes.md` (audit spec); item 1.5
there is a hard prerequisite of this feature.

## Problem

django-denorm-iplweb has no way to declare that a denormalized function
depends on *same-model* fields — a sibling column (`full_name` reads
`first_name`) or a sibling denormalized field (`letterhead` reads
`full_name`). `@depend_on_related` only tracks related models.

Today this gap is papered over by the catch-all self-trigger: any change to
any watched column inserts a `(content_type, object_id, func_name=NULL)`
marker, and `flush` treats NULL as "recompute every denormalized field on
this object" via a full `save()`. Consequences:

* Every ORM save and every bulk update causes a redundant whole-object
  re-save during flush.
* Same-model chains (denorm reading denorm) converge only via extra passes
  and are sensitive to field declaration order.
* The trigger layer cannot express "only `full_name` is dirty".

## Decision summary (per design review)

1. **NULL keeps its meaning and precedence.** `func_name=NULL` means
   "whole object dirty". When markers `{f1, f2, NULL}` coexist for one
   object, NULL wins and flush performs a full save. This is existing
   `flush_single` behavior (`if None not in func_names`) — promoted to a
   documented, regression-tested contract.
2. **The library's triggers stop emitting NULL.** Self-triggers become
   per-function and insert `(ct, oid, '<func_name>')` markers.
3. **NULL becomes explicit-only.** Emitted by `rebuild_instances_of()`
   (a rebuild is whole-object by definition) and by a new public
   `denorm.mark_dirty(*instances)` helper.
4. Functions that do not declare dependencies lose nothing: they get a
   conservative per-function trigger equivalent to today's catch-all,
   addressed to the function instead of the whole object.

## Public API

```python
from denorm import denormalized, depend_on_fields

class Person(models.Model):
    first_name = models.CharField(max_length=50)
    last_name = models.CharField(max_length=50)

    @denormalized(models.CharField, max_length=101)
    @depend_on_fields("first_name", "last_name")
    def full_name(self):
        return f"{self.first_name} {self.last_name}"

    @denormalized(models.CharField, max_length=110)
    @depend_on_fields("full_name")        # sibling denorm field
    def letterhead(self):
        return f"Dear {self.full_name}"
```

* `depend_on_fields(*field_names)` is a stacked decorator, symmetric with
  `@depend_on_related`. It appends a `DependOnFields` dependency to
  `func.depend` via the existing `make_depend_decorator` machinery
  (`denorm/dependencies.py`).
* Field names refer to concrete columns of the same model, by attname
  (sibling denormalized fields are ordinary columns at trigger level).
* Stacking several `@depend_on_fields` on one function is allowed; their
  conditions union (logical OR).
* `@depend_on_fields()` with **no arguments** is meaningful: "this function
  reads no sibling columns" (e.g. it only aggregates related models). Such
  a function gets **no UPDATE self-trigger** at all.
* New helper `denorm.mark_dirty(*instances)`: bulk-creates
  `(ct, pk, func_name=NULL)` markers with `ignore_conflicts=True`.
  Exported from `denorm/__init__.py`. Queryset-shaped whole-object marking
  remains `rebuild_instances_of(model, *args, **kwargs)`.
* Decorator order: `@denormalized` must be outermost (same contract as
  `@depend_on_related`). A `@depend_on_fields` on a function never wrapped
  by `@denormalized` is inert (cannot be detected — documented).

## Trigger generation

`CallbackDenorm.get_triggers` (`denorm/denorms.py`) stops emitting the
catch-all NULL pair. Per `@denormalized` function it emits:

| Case | UPDATE trigger | INSERT trigger |
|---|---|---|
| Declared deps | condition: any *declared* column changed (`OLD.c IS DISTINCT FROM NEW.c` OR-chain) → insert `(ct, NEW.pk, 'func')` | unconditional insert `(ct, NEW.pk, 'func')` |
| No declaration | condition: any *watched* column changed (today's skip/only semantics) → insert `(ct, NEW.pk, 'func')` | same |
| `@depend_on_fields()` (empty) | no UPDATE trigger | same |

Details:

* Trigger naming uses the existing func-suffix mechanism
  (`denorm/db/triggers.py` `Trigger.name`), exactly as dependency triggers
  already do. Per-function names mean per-function conditions never merge —
  the `TriggerSet.append` "first watch-list wins" wart no longer applies to
  self-triggers.
* INSERT triggers are unconditional for **every** function (OLD/NEW
  comparison impossible on insert; covers raw-SQL inserts — the same
  conservatism as today's unconditional NULL, but targeted). Shape: emit
  the per-function INSERT actions **without** a func-suffixed name so
  `TriggerSet.append` merges them into one trigger function per table
  containing N marker inserts (safe here — the merge wart concerns
  *conditions*, and INSERT self-triggers have none). UPDATE self-triggers
  keep func-suffixed names and stay separate.
* Implementation split: `DependOnFields` (new class,
  `denorm/dependencies.py`) carries declared names and produces the
  targeted triggers from `get_triggers()`; `CallbackDenorm.get_triggers`
  inspects its `depend` list — if a `DependOnFields` is present it emits no
  conservative trigger of its own, otherwise it emits the conservative
  per-function pair.
* `DENORM_BULK_UNSAFE_TRIGGERS = True` (`BaseCallbackDenorm`) keeps meaning
  "no self-triggers at all" — unchanged.
* `skip` / `only` on `@denormalized` keep their current meaning for the
  conservative (undeclared) case. For declared functions they are ignored
  for the self-trigger (the declaration *is* the watch-list); they still
  apply to `@depend_on_related` triggers as today.
* Dedup is unchanged: markers dedup per `(ct, COALESCE(oid,-1),
  COALESCE(func_name,''))` under the 0017 unique index.

Backward compatibility: a model with no declarations gets per-function
conservative triggers whose union of firing conditions equals today's
catch-all. Flush then saves with `update_fields=[all denorm fields]`
instead of a full save — same recomputation, smaller UPDATE.

## Flush path

* **No precedence code changes**: NULL-wins already exists in
  `flush_single`; it gets a pinning regression test.
* **Unknown func_name fallback already correct**: a marker naming a field
  that no longer exists (stale rows across deploys) falls through to a full
  save. Pinned by test.
* **Hard prerequisite — audit spec item 1.5 (delete claimed markers at
  claim time).** Cascades are the heart of this feature: flush writes
  `full_name`, the per-function trigger marks `letterhead`. The current
  swallow bug eats exactly that marker whenever an identical one was
  claimed in the same round. 1.5 ships first (with its staged-race tests).
* The 2.5 convergence loop (audit spec) composes naturally: fresh
  same-object markers claimed by later loop iterations now carry specific
  func names, so iterations stay targeted instead of escalating to full
  saves.
* Side effect worth release-noting: targeted `update_fields` saves do not
  touch `auto_now` columns, so `DENORM_DISABLE_AUTOTIME_DURING_FLUSH`
  becomes relevant only for explicit-NULL full saves.

## Scanner — Django system check

New `denorm/checks.py`, registered in `DenormConfig.ready`
(`denorm/apps.py`).

Mechanics: for each `@denormalized` function — `inspect.getsource` →
`ast.parse` → collect `self.<attr>` attribute reads → intersect with the
model's concrete field names (excluding the function's own field) → diff
against declarations. Follows one level of `self._helper()` calls into
methods defined on the same class to reduce false "cannot verify" noise.

| Check | Severity | Meaning |
|---|---|---|
| `denorm.E001` | Error | Function **has** declarations but reads an undeclared sibling column — declared precision is broken; real staleness bug. |
| `denorm.E002` | Error | A declared name is not a concrete field of the model, or names the function's own field (self-dependency). Also raised at trigger-build time with a message listing available fields (mirrors existing `only`/`skip` validation). |
| `denorm.W001` | Warning | Function has **no** declarations and reads sibling columns — "consider `@depend_on_fields(...)`". Correctness still covered by the conservative trigger. |
| `denorm.W002` | Warning | Declared function uses `getattr(self, ...)` or calls that the scanner cannot follow — "cannot fully verify declarations". |

Known blind spots (documented): dynamic access, properties, multi-level
helper indirection. The scanner is a linter — sound on what it reports,
incomplete on what it can see. `SILENCED_SYSTEM_CHECKS` opts out per check.

## Error handling

* `@depend_on_fields("typo")` → `denorm.E002` at `manage.py check`;
  `FieldDoesNotExist` with available-field listing at
  `denorm_init`/`denorm_rebuild_triggers` time.
* `@depend_on_fields("itself")` (function's own field) → `denorm.E002` /
  `ValueError` at build time.
* Empty declarations list vs no decorator are distinct states (empty =
  "reads nothing", absent = "unknown → conservative trigger").

## Testing

TDD throughout (failing test first, per project practice). PostgreSQL
functional tests in the existing test-app/test_deadlocks styles:

1. Bulk update of a declared source column → exactly one targeted marker,
   **no NULL row** in `denorm_dirtyinstance`.
2. Flush of that marker recomputes only the declared field (sentinel value
   on the other denorm column asserts it was not rewritten).
3. Chain `full_name → letterhead` converges **regardless of field
   declaration order** (the order-sensitivity bug class dies).
4. `mark_dirty()` emits NULL; flush performs a full save.
5. Mixed markers `{field, NULL}` → NULL precedence (pins existing
   behavior).
6. Marker with unknown func_name → full-save fallback (pins existing
   behavior).
7. Raw-SQL INSERT of a row → per-function markers → flush heals all denorm
   fields.
8. Undeclared model: any watched column change marks every function;
   behavior equivalent to today's catch-all (regression parity).
9. Scanner: complete declaration (silent), incomplete declaration (E001),
   undeclared reads (W001), dynamic access (W002), bad name (E002).
10. Full existing suite stays green (`python runtests.py postgres`,
    `pytest tests/`).

## Sequencing and rollout

1. Commit 1: audit spec item 1.5 (delete-at-claim) + staged-race tests.
2. Commits 2..n: this feature, TDD, one logical change per commit
   (decorator + dependency class → trigger generation → mark_dirty →
   scanner → docs).
3. Version 1.12.0. Release notes: run `denorm_rebuild_triggers` after
   upgrade (trigger SQL changes shape); NULL contract documented;
   `DENORM_DISABLE_AUTOTIME_DURING_FLUSH` scope note.

## Out of scope

* Suppressing markers for ORM saves entirely (triggers still cannot
  distinguish ORM writes from bulk writes; targeted markers shrink the
  redundancy from "whole object" to "per function").
* Runtime read-tracing for the scanner (proxy-instance execution) — a
  possible later upgrade.
* Scanner coverage of related-model reads (`self.fk.x`,
  `self.related_set`) — belongs to `@depend_on_related` auditing, separate
  effort.
