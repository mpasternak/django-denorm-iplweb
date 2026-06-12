# @depend_on_fields Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Declarative same-model dependencies for denormalized fields: per-function dirty markers replace the catch-all `func_name=NULL` self-trigger; NULL becomes explicit-only (`mark_dirty`, rebuild) with documented precedence; an AST system check audits declarations. Prerequisite bugfix: delete claimed markers at claim time (audit spec item 1.5).

**Architecture:** `@depend_on_fields` rides the existing `func.depend` decorator machinery (like `@depend_on_related`) as a new `DependOnFields` dependency class that emits a targeted UPDATE self-trigger per declared function. `CallbackDenorm.get_triggers` stops emitting NULL markers: it emits one merged unconditional INSERT trigger (per-function markers) plus a conservative watch-all UPDATE trigger only for undeclared functions. `flush_single` is reordered to lock-object → claim-and-delete-markers → save, closing the marker-swallow race introduced by the 0017 unique index.

**Tech Stack:** Django 4.2+/5.2+, PostgreSQL 12+, plpgsql triggers, pytest + pytest-django (`tests/`), Django test runner (`test_denorm_project/`), `ast`/`inspect` for the scanner.

**Reference docs:**
- Design (approved): `docs/superpowers/specs/2026-06-12-depend-on-fields-design.md`
- Audit spec item 1.5: `docs/spec-concurrency-performance-fixes.md`

**Conventions you must know:**
- Run pytest suite: `pytest tests/ -x -q` (needs local PostgreSQL; uses fixtures `transactional_db`, `denorm_triggers`, `thread_runner` from `tests/conftest.py`).
- Run Django suite: `python runtests.py postgres` (from repo root).
- Flake8 max line length is 120: `flake8 denorm/ tests/`.
- `DirtyInstance` markers dedup on unique index `(content_type_id, COALESCE(object_id,-1), COALESCE(func_name,''))` — an INSERT colliding with an existing committed row is silently dropped by the trigger's `EXCEPTION WHEN unique_violation` handler.
- Trigger names: `base.Trigger.name()` = `d_<time>_row_<evt>_on_<table>`; the PG subclass appends `_<func.__qualname__>` when a `func` is passed (`denorm/db/triggers.py:61-68`). Same-named triggers are merged by `TriggerSet.append` (actions concatenated, first trigger's condition fields win).

---

### Task 1: Failing tests for item 1.5 (delete claimed markers at claim time)

**Files:**
- Modify: `tests/test_deadlocks.py` (append at end)

- [ ] **Step 1: Append the two failing tests**

Append to `tests/test_deadlocks.py` (it already imports `ContentType` from `django.contrib.contenttypes.models`, `patch` from `unittest.mock` at the top — check the import block and add any missing: `import threading`, `import time` are needed; check whether they're already imported and add only what's missing):

```python
# ---------------------------------------------------------------------------
# 11. Item 1.5: claimed markers must be deleted at claim time, not commit.
# See docs/spec-concurrency-performance-fixes.md item 1.5.
# ---------------------------------------------------------------------------


def test_flush_single_deletes_claimed_markers_before_save(
    transactional_db, denorm_triggers
):
    """The unique-index dedup silently drops marker INSERTs that collide
    with a row that already exists. flush_single keeps its claimed markers
    alive until the end of its transaction, so any identical marker
    inserted while it works (by its own save's triggers, or by a
    concurrent writer) is swallowed and the invalidation is lost.

    Fix: flush_single deletes the claimed rows immediately after claiming
    them (inside the transaction). This test pins the observable core of
    the fix: by the time obj.save() runs, the claimed markers are gone.
    """
    from test_app.models import Forum

    from denorm import denorms
    from denorm.models import DirtyInstance

    forum = Forum.objects.create(title="claim-time")
    forum_ct = ContentType.objects.get_for_model(Forum)
    DirtyInstance.objects.all().delete()
    DirtyInstance.objects.create(content_type=forum_ct, object_id=forum.pk)

    seen = {}
    orig_save = Forum.save

    def spying_save(self, *args, **kwargs):
        seen["markers_at_save_time"] = DirtyInstance.objects.filter(
            content_type=forum_ct, object_id=self.pk
        ).count()
        return orig_save(self, *args, **kwargs)

    with patch.object(Forum, "save", spying_save):
        denorms.flush_single(forum_ct.pk, forum.pk, forum_ct)

    assert seen["markers_at_save_time"] == 0, (
        "flush_single ran obj.save() while its claimed DirtyInstance rows "
        "still existed. While they exist, the unique index silently drops "
        "any identical marker inserted by this save's own triggers or by "
        "concurrent writers — losing invalidations. Claimed markers must "
        "be deleted at claim time (audit spec item 1.5)."
    )
    assert not DirtyInstance.objects.filter(
        content_type=forum_ct, object_id=forum.pk
    ).exists()


def test_concurrent_marker_survives_inflight_flush(
    transactional_db, denorm_triggers
):
    """Swallow race, end to end. While flush_single holds claimed marker
    (ct_forum, forum.pk, 'author_names'), a concurrent writer updates a
    Post; the dependency trigger inserts the same logical marker.

    Before the fix: the claimed row still exists -> unique_violation ->
    trigger handler swallows the insert -> flush deletes its claimed rows
    and commits -> the writer's invalidation is GONE (and this flush may
    have recomputed BEFORE the writer committed).

    After the fix: the claimed row is already deleted (uncommitted) ->
    the writer's insert waits for our commit -> lands AFTER it -> a fresh
    marker survives for the next round.
    """
    from django.db import connections

    from test_app.models import Forum, Post

    from denorm import denorms
    from denorm.models import DirtyInstance

    forum = Forum.objects.create(title="race-forum")
    post = Post.objects.create(forum=forum, title="orig")
    denorms.flush()  # settle all markers from setup
    assert not DirtyInstance.objects.exists()

    # Stage 1: one claimed-to-be marker for (forum, 'author_names').
    forum_ct = ContentType.objects.get_for_model(Forum)
    DirtyInstance.objects.create(
        content_type=forum_ct, object_id=forum.pk, func_name="author_names"
    )

    writer_done = threading.Event()

    def concurrent_writer():
        # Own thread = own Django connection (autocommit). Updating the
        # post row fires Forum's dependency trigger, inserting
        # (ct_forum, forum.pk, 'author_names') — colliding with the
        # marker the flush worker claimed.
        try:
            Post.objects.filter(pk=post.pk).update(title="changed-mid-flush")
            writer_done.set()
        finally:
            for alias in connections:
                try:
                    connections[alias].close()
                except Exception:
                    pass

    writer = threading.Thread(target=concurrent_writer, daemon=True)

    orig_save = Forum.save

    def save_with_concurrent_write(self, *args, **kwargs):
        writer.start()
        # Give the writer time to reach the marker INSERT. Before the
        # fix it completes instantly (insert swallowed). After the fix
        # it blocks on our uncommitted delete until we commit.
        time.sleep(1.0)
        return orig_save(self, *args, **kwargs)

    with patch.object(Forum, "save", save_with_concurrent_write):
        denorms.flush_single(forum_ct.pk, forum.pk, forum_ct)

    writer.join(timeout=30)
    assert writer_done.is_set(), "concurrent writer never finished — hung lock?"

    assert DirtyInstance.objects.filter(
        content_type=forum_ct, object_id=forum.pk, func_name="author_names"
    ).exists(), (
        "The concurrent writer's invalidation marker was swallowed by the "
        "unique-index dedup while flush_single held an identical claimed "
        "marker. The denormalized value is now silently stale. Claimed "
        "markers must be deleted at claim time so colliding inserts wait "
        "for our commit instead of being dropped (audit spec item 1.5)."
    )
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `pytest tests/test_deadlocks.py -k "claim_time or claimed_markers_before_save or concurrent_marker_survives" -v`

(Adjust `-k` to the actual test names: `test_flush_single_deletes_claimed_markers_before_save`, `test_concurrent_marker_survives_inflight_flush`.)

Expected: both FAIL.
- First: `markers_at_save_time == 1` (marker still present at save time).
- Second: the final `DirtyInstance.objects.filter(...).exists()` assertion fails (marker swallowed).

If the second test fails for a different reason (e.g. writer timeout), investigate before proceeding — it must fail on the swallow assertion to prove it reproduces the race.

- [ ] **Step 3: Commit the failing tests**

```bash
git add tests/test_deadlocks.py
git commit -m "test: reproduce marker-swallow race from delayed claimed-marker delete (spec 1.5)"
```

---

### Task 2: Implement item 1.5 — claim-and-delete in `flush_single`

**Files:**
- Modify: `denorm/denorms.py:886-966` (`flush_single`)

- [ ] **Step 1: Add the claim helper and reorder `flush_single`**

In `denorm/denorms.py`, immediately above `flush_single`, add:

```python
def _claim_func_names(content_type_id, object_id):
    """Lock, snapshot and DELETE all claimable markers for the pair.

    Returns (claimed_any, func_names). Deleting at claim time (inside the
    caller's transaction) frees the unique-index key, so a colliding
    marker INSERT — from this transaction's own save() triggers or from a
    concurrent writer — waits for our commit instead of being silently
    dropped by the unique_violation handler.
    See docs/spec-concurrency-performance-fixes.md item 1.5.
    """
    from .models import DirtyInstance

    locked_pks = list(
        DirtyInstance.objects.filter(
            content_type_id=content_type_id, object_id=object_id
        )
        .select_for_update(skip_locked=True)
        .values_list("pk", flat=True)
    )
    if not locked_pks:
        return False, set()
    func_names = set(
        DirtyInstance.objects.filter(pk__in=locked_pks).values_list(
            "func_name", flat=True
        )
    )
    DirtyInstance.objects.filter(pk__in=locked_pks).delete()
    return True, func_names
```

Replace the body of `flush_single`'s `with transaction.atomic():` block. The current order is claim → lock object → read func_names → save → delete. The new order is lock object → claim-and-delete → save:

```python
@retry_on_serialization_failure
def flush_single(content_type_id, object_id, content_type=None):
    from denorm.conf import settings

    from .models import DirtyInstance

    disable_autotime_during_flush = settings.DENORM_DISABLE_AUTOTIME_DURING_FLUSH
    autotime_field_names = settings.DENORM_AUTOTIME_FIELD_NAMES

    if content_type is None:
        from django.contrib.contenttypes.models import ContentType

        content_type = ContentType.objects.get(pk=content_type_id)

    with transaction.atomic():
        klass = content_type.model_class()

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
            _claim_func_names(content_type.pk, object_id)
            return

        # Claim AND DELETE the markers now, before save(): while a claimed
        # marker still exists, the unique index silently swallows identical
        # marker inserts (our own save's triggers, concurrent writers),
        # losing invalidations.
        claimed, func_names = _claim_func_names(content_type.pk, object_id)
        if not claimed:
            return

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

        obj.save(**kw)
```

Note the lock-order change (object row first, then markers): all marker/object acquisitions use `skip_locked`, so no flush worker ever *waits* on either resource — the inversion cannot introduce a flush-vs-flush deadlock.

- [ ] **Step 2: Run the Task 1 tests to verify they pass**

Run: `pytest tests/test_deadlocks.py -k "claimed_markers_before_save or concurrent_marker_survives" -v`
Expected: 2 passed.

- [ ] **Step 3: Run both full suites**

Run: `pytest tests/ -q` — Expected: all pass.
Run: `python runtests.py postgres` — Expected: all pass (flush semantics are unchanged for callers: same markers consumed, same save).

- [ ] **Step 4: Commit**

```bash
git add denorm/denorms.py
git commit -m "fix: delete claimed DirtyInstance markers at claim time (spec 1.5)

While claimed markers stayed alive until end of transaction, the 0017
unique index silently swallowed any identical marker inserted mid-flush
(own save's triggers, concurrent writers), losing invalidations."
```

---

### Task 3: `DependOnFields` dependency class and `depend_on_fields` decorator

**Files:**
- Modify: `denorm/dependencies.py` (add class + decorator at end, where `depend_on_related` is defined)
- Modify: `denorm/__init__.py` (export)
- Create: `tests/test_depend_on_fields.py`

- [ ] **Step 1: Write the failing unit tests**

Create `tests/test_depend_on_fields.py`:

```python
"""Tests for @depend_on_fields — declarative same-model dependencies.

Design: docs/superpowers/specs/2026-06-12-depend-on-fields-design.md
"""

from __future__ import annotations

import pytest
from django.core.exceptions import FieldDoesNotExist


def _named_func(name):
    """A stand-in for a @denormalized function with a given name.

    __qualname__ matters too: the PG Trigger.name() builds the trigger
    name from func.__qualname__, and a nested test function would leak
    '<locals>' into it.
    """

    def func(self):
        return ""

    func.__name__ = name
    func.__qualname__ = name
    return func


class TestDependOnFieldsValidation:
    def test_unknown_field_name_raises_at_trigger_build(self, db):
        from test_app.models import Member

        from denorm.dependencies import DependOnFields

        dep = DependOnFields("no_such_field", func=_named_func("full_name"))
        dep.setup(Member)
        with pytest.raises(FieldDoesNotExist) as exc:
            dep.get_triggers(using=None)
        assert "no_such_field" in str(exc.value)
        # The message must list available fields to be actionable.
        assert "first_name" in str(exc.value)

    def test_own_field_self_dependency_raises(self, db):
        from test_app.models import Member

        from denorm.dependencies import DependOnFields

        dep = DependOnFields("full_name", func=_named_func("full_name"))
        dep.setup(Member)
        with pytest.raises(ValueError) as exc:
            dep.get_triggers(using=None)
        assert "full_name" in str(exc.value)

    def test_empty_declaration_emits_no_triggers(self, db):
        from test_app.models import Member

        from denorm.dependencies import DependOnFields

        dep = DependOnFields(func=_named_func("full_name"))
        dep.setup(Member)
        assert dep.get_triggers(using=None) == []


class TestDependOnFieldsTriggerShape:
    def test_targeted_update_trigger(self, db):
        from test_app.models import Member

        from denorm.dependencies import DependOnFields

        dep = DependOnFields("first_name", "name", func=_named_func("full_name"))
        dep.setup(Member)
        triggers = dep.get_triggers(using=None)

        assert len(triggers) == 1
        trigger = triggers[0]
        assert trigger.event == "update"
        # Watch-list is exactly the declared columns.
        assert sorted(f for f, _ in trigger.fields) == ["first_name", "name"]
        # func is set -> per-function trigger name, never merged with others.
        assert "full_name" in trigger.name()

        sql, params = trigger.actions[0].sql()
        assert "func_name" in sql
        assert "'full_name'" in sql
        assert "NULL" not in sql.upper().replace("ON CONFLICT", "")


class TestDecorator:
    def test_decorator_attaches_dependency_info(self):
        from denorm.dependencies import DependOnFields, depend_on_fields

        @depend_on_fields("first_name", "last_name")
        def full_name(self):
            return ""

        assert len(full_name.depend) == 1
        cls, args, kwargs = full_name.depend[0]
        assert cls is DependOnFields
        assert args == ("first_name", "last_name")
        assert kwargs["func"] is full_name

    def test_exported_from_package_root(self):
        import denorm

        assert callable(denorm.depend_on_fields)
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_depend_on_fields.py -v`
Expected: FAIL / ERROR with `ImportError: cannot import name 'DependOnFields'`.

- [ ] **Step 3: Implement `DependOnFields` and the decorator**

In `denorm/dependencies.py`, add after the `CallbackDependOnRelated` class (before `make_depend_decorator`):

```python
class DependOnFields(DenormDependency):
    """Same-model dependency: the decorated function reads the declared
    sibling columns of its own model instance (plain columns or other
    denormalized fields).

    Emits one targeted AFTER UPDATE trigger that fires only when a
    declared column changes, inserting a per-function dirty marker
    (content_type_id, object_id, '<function name>').

    An EMPTY declaration (``@depend_on_fields()``) means "this function
    reads no sibling columns" and emits no UPDATE trigger at all.
    The unconditional INSERT marker is emitted by CallbackDenorm for
    every function regardless of declarations.
    """

    def __init__(self, *field_names, func=None):
        self.field_names = tuple(field_names)
        self.func = func

    def resolve_attnames(self):
        """Map declared names (field name or attname) to column attnames.

        Raises FieldDoesNotExist for unknown names, ValueError for a
        self-dependency on the function's own field.
        """
        concrete = list(self.this_model._meta.concrete_fields)
        by_either_name = {}
        for f in concrete:
            by_either_name[f.name] = f.attname
            by_either_name[f.attname] = f.attname

        own = self.func.__name__
        resolved = []
        for name in self.field_names:
            if name == own:
                raise ValueError(
                    f"@depend_on_fields on {self.this_model.__name__}.{own} "
                    f'declares its own field "{name}" — a denormalized '
                    f"function cannot depend on itself."
                )
            try:
                resolved.append(by_either_name[name])
            except KeyError:
                available = ", ".join(sorted({f.attname for f in concrete}))
                raise FieldDoesNotExist(
                    f'Field name "{name}", declared in @depend_on_fields of '
                    f"{self.this_model.__name__}.{own}, does not exist. "
                    f"Field names available: {available}"
                )
        return resolved

    def get_triggers(self, using):
        if not self.field_names:
            return []

        from denorm.db import triggers

        attnames = self.resolve_attnames()
        qn = self.get_quote_name(using)
        content_type = str(
            contenttypes.models.ContentType.objects.get_for_model(self.this_model).pk
        )
        action = triggers.TriggerActionInsert(
            model=denorm.models.DirtyInstance,
            columns=("content_type_id", "object_id", "func_name"),
            values=(
                content_type,
                "NEW.%s" % qn(self.this_model._meta.pk.get_attname_column()[1]),
                # func.__name__ is a Python identifier — safe to inline.
                "'%s'" % self.func.__name__,
            ),
        )
        return [
            triggers.Trigger(
                self.this_model,
                "after",
                "update",
                [action],
                content_type,
                using,
                None,
                tuple(attnames),
                self.func,
            )
        ]
```

`FieldDoesNotExist` import: `dependencies.py` currently doesn't import it — add at the top:

```python
from django.core.exceptions import FieldDoesNotExist
```

At the bottom of `dependencies.py`, next to `depend_on_related = make_depend_decorator(CallbackDependOnRelated)`, add:

```python
depend_on_fields = make_depend_decorator(DependOnFields)
```

Note: `make_depend_decorator` calls `functools.update_wrapper(decorator, Class.__init__)` and injects `kwargs["func"] = func` — `DependOnFields.__init__` accepts `func=` accordingly, no decorator-machinery changes needed.

In `denorm/__init__.py` change the dependencies import line and `__all__`:

```python
from .dependencies import depend_on_fields, depend_on_related
```

```python
__all__ = [
    "cached",
    "denormalized",
    "depend_on_fields",
    "depend_on_related",
    "flush",
    "rebuildall",
    "CountField",
    "CacheKeyField",
    "retry_on_serialization_failure",
]
```

(`mark_dirty` joins this list in Task 6, together with its import.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_depend_on_fields.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add denorm/dependencies.py denorm/__init__.py tests/test_depend_on_fields.py
git commit -m "feat: add @depend_on_fields declarative same-model dependencies"
```

---

### Task 4: Test models and migration

**Files:**
- Modify: `test_denorm_project/test_app/models.py` (append)
- Create: `test_denorm_project/test_app/migrations/0009_*.py` (generated)

- [ ] **Step 1: Add the feature test models**

Append to `test_denorm_project/test_app/models.py`. Also extend the denorm import at the top of the file (`from denorm import CacheKeyField, CountField, cached, denormalized, depend_on_related`) with `depend_on_fields`:

```python
class Profile(models.Model):
    """@depend_on_fields feature model: declared deps + denorm->denorm chain."""

    first_name = models.CharField(max_length=50)
    last_name = models.CharField(max_length=50)
    nickname = models.CharField(max_length=50, default="")

    @denormalized(models.CharField, max_length=101)
    @depend_on_fields("first_name", "last_name")
    def full_name(self):
        return f"{self.first_name} {self.last_name}"

    @denormalized(models.CharField, max_length=120)
    @depend_on_fields("full_name")
    def letterhead(self):
        return f"Dear {self.full_name}"


class ProfileReversed(models.Model):
    """Same as Profile but with the chained field DECLARED FIRST — the
    declaration-order-sensitivity case that the catch-all NULL design got
    wrong (stale letterhead) and per-function markers must get right."""

    first_name = models.CharField(max_length=50)
    last_name = models.CharField(max_length=50)

    @denormalized(models.CharField, max_length=120)
    @depend_on_fields("full_name")
    def letterhead(self):
        return f"Dear {self.full_name}"

    @denormalized(models.CharField, max_length=101)
    @depend_on_fields("first_name", "last_name")
    def full_name(self):
        return f"{self.first_name} {self.last_name}"


class UndeclaredProfile(models.Model):
    """No declarations: must get the conservative per-function trigger
    (today's catch-all semantics, addressed to the function)."""

    first_name = models.CharField(max_length=50)
    last_name = models.CharField(max_length=50)

    @denormalized(models.CharField, max_length=101)
    def full_name(self):
        return f"{self.first_name} {self.last_name}"
```

Note on `ProfileReversed.letterhead`: reading `self.full_name` before the
`full_name` field is defined in the class body is fine — the read happens at
runtime on instances, not at class-definition time.

- [ ] **Step 2: Generate the migration**

```bash
cd test_denorm_project && DJANGO_SETTINGS_MODULE=test_denorm_project.settings_postgres python manage.py makemigrations test_app && cd ..
```

Expected output: `Migrations for 'test_app': ... 0009_profile_profilereversed_undeclaredprofile.py` (name may vary; must contain all three models and nothing else — if it tries to delete `FilterSumModel`/`FilterCountModel` you are running under sqlite settings; fix `DJANGO_SETTINGS_MODULE`).

- [ ] **Step 3: Sanity-run both suites (schema applies, triggers build)**

Run: `python runtests.py postgres` — Expected: pass (post-migrate hook installs triggers for the new models; `@depend_on_fields` triggers are emitted by `DependOnFields.get_triggers` via the denorm's `depend` list).
Run: `pytest tests/test_depend_on_fields.py -q` — Expected: pass.

- [ ] **Step 4: Commit**

```bash
git add test_denorm_project/test_app/models.py test_denorm_project/test_app/migrations/
git commit -m "test: add Profile/ProfileReversed/UndeclaredProfile feature models"
```

---

### Task 5: Per-function self-triggers in `CallbackDenorm` (kill the NULL catch-all)

**Files:**
- Modify: `denorm/denorms.py:195-248` (`CallbackDenorm.get_triggers`)
- Modify: `tests/test_depend_on_fields.py` (append functional tests)

- [ ] **Step 1: Write the failing functional tests**

Append to `tests/test_depend_on_fields.py`:

```python
from django.contrib.contenttypes.models import ContentType


def _markers(model, pk):
    from denorm.models import DirtyInstance

    ct = ContentType.objects.get_for_model(model)
    return DirtyInstance.objects.filter(content_type=ct, object_id=pk)


class TestPerFunctionSelfTriggers:
    def test_bulk_update_emits_targeted_markers_not_null(
        self, transactional_db, denorm_triggers
    ):
        from test_app.models import Profile

        from denorm.models import DirtyInstance

        p = Profile.objects.create(first_name="John", last_name="Doe")
        DirtyInstance.objects.all().delete()

        Profile.objects.filter(pk=p.pk).update(last_name="Smith")

        func_names = set(_markers(Profile, p.pk).values_list("func_name", flat=True))
        assert None not in func_names, (
            "Library trigger emitted a func_name=NULL (whole-object) marker. "
            "NULL is reserved for explicit mark_dirty()/rebuild."
        )
        assert "full_name" in func_names  # declared on last_name
        assert "letterhead" not in func_names  # declared only on full_name column

    def test_flush_recomputes_only_the_marked_field(
        self, transactional_db, denorm_triggers
    ):
        from test_app.models import Profile

        from denorm import denorms
        from denorm.models import DirtyInstance

        p = Profile.objects.create(first_name="John", last_name="Doe")
        denorms.flush()
        DirtyInstance.objects.all().delete()

        # Corrupt letterhead's column directly (no trigger watches it),
        # then dirty ONLY full_name via its declared source column.
        Profile.objects.filter(pk=p.pk).update(letterhead="SENTINEL")
        assert not DirtyInstance.objects.exists()
        Profile.objects.filter(pk=p.pk).update(last_name="Smith")

        ct = ContentType.objects.get_for_model(Profile)
        denorms.flush_single(ct.pk, p.pk, ct)

        p.refresh_from_db()
        assert p.full_name == "John Smith"
        assert p.letterhead == "SENTINEL", (
            "flush_single(update_fields=['full_name']) must not rewrite the "
            "letterhead column — targeted markers mean targeted saves."
        )

    def test_chain_cascades_and_full_flush_converges(
        self, transactional_db, denorm_triggers
    ):
        from test_app.models import Profile

        from denorm import denorms
        from denorm.models import DirtyInstance

        p = Profile.objects.create(first_name="John", last_name="Doe")
        denorms.flush()
        DirtyInstance.objects.all().delete()

        Profile.objects.filter(pk=p.pk).update(last_name="Smith")
        ct = ContentType.objects.get_for_model(Profile)

        # First targeted flush changes the full_name COLUMN, whose trigger
        # must cascade a 'letterhead' marker (needs the 1.5 fix to not be
        # swallowed when processed in the same outer loop).
        denorms.flush_single(ct.pk, p.pk, ct)
        cascade = set(_markers(Profile, p.pk).values_list("func_name", flat=True))
        assert "letterhead" in cascade

        denorms.flush()
        p.refresh_from_db()
        assert p.full_name == "John Smith"
        assert p.letterhead == "Dear John Smith"
        assert not DirtyInstance.objects.exists()

    def test_chain_converges_regardless_of_declaration_order(
        self, transactional_db, denorm_triggers
    ):
        from test_app.models import ProfileReversed

        from denorm import denorms
        from denorm.models import DirtyInstance

        p = ProfileReversed.objects.create(first_name="John", last_name="Doe")
        denorms.flush()
        DirtyInstance.objects.all().delete()

        ProfileReversed.objects.filter(pk=p.pk).update(last_name="Smith")
        denorms.flush()

        p.refresh_from_db()
        assert p.full_name == "John Smith"
        assert p.letterhead == "Dear John Smith", (
            "Chained denorm declared BEFORE its source stayed stale. "
            "Per-function markers must make convergence independent of "
            "field declaration order."
        )

    def test_raw_sql_insert_marks_every_function(
        self, transactional_db, denorm_triggers
    ):
        from django.db import connection

        from test_app.models import Profile

        from denorm import denorms

        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO test_app_profile "
                "(first_name, last_name, nickname, full_name, letterhead) "
                "VALUES ('Raw', 'Insert', '', '', '') RETURNING id"
            )
            pk = cursor.fetchone()[0]

        func_names = set(_markers(Profile, pk).values_list("func_name", flat=True))
        assert func_names == {"full_name", "letterhead"}

        denorms.flush()
        p = Profile.objects.get(pk=pk)
        assert p.full_name == "Raw Insert"
        assert p.letterhead == "Dear Raw Insert"

    def test_undeclared_function_gets_conservative_targeted_marker(
        self, transactional_db, denorm_triggers
    ):
        from test_app.models import UndeclaredProfile

        from denorm import denorms
        from denorm.models import DirtyInstance

        p = UndeclaredProfile.objects.create(first_name="John", last_name="Doe")
        denorms.flush()
        DirtyInstance.objects.all().delete()

        # nickname-free model: ANY column change marks the function —
        # catch-all semantics, but addressed.
        UndeclaredProfile.objects.filter(pk=p.pk).update(first_name="Jane")
        func_names = set(
            _markers(UndeclaredProfile, p.pk).values_list("func_name", flat=True)
        )
        assert func_names == {"full_name"}

        denorms.flush()
        p.refresh_from_db()
        assert p.full_name == "Jane Doe"
        assert not DirtyInstance.objects.exists()


class TestTriggerSetShape:
    def test_profile_triggerset_shape(self, db):
        from denorm.denorms import build_triggerset

        ts = build_triggerset()
        profile_triggers = {
            name: t
            for name, t in ts.triggers.items()
            if t.db_table == "test_app_profile"
        }

        updates = {n: t for n, t in profile_triggers.items() if t.event == "update"}
        inserts = {n: t for n, t in profile_triggers.items() if t.event == "insert"}

        # Two per-function UPDATE triggers (func-suffixed names).
        assert len(updates) == 2
        assert any("full_name" in n for n in updates)
        assert any("letterhead" in n for n in updates)
        # Watch-lists are exactly the declared columns.
        for name, t in updates.items():
            cols = sorted(f for f, _ in t.fields)
            if "letterhead" in name:
                assert cols == ["full_name"]
            else:
                assert cols == ["first_name", "last_name"]

        # ONE merged INSERT trigger carrying both functions' marker actions.
        assert len(inserts) == 1
        insert_trigger = next(iter(inserts.values()))
        action_sqls = [a.sql()[0] for a in insert_trigger.actions]
        assert any("'full_name'" in s for s in action_sqls)
        assert any("'letterhead'" in s for s in action_sqls)

    def test_no_self_trigger_inserts_null_func_name(self, db):
        from denorm.denorms import build_triggerset
        from denorm.models import DirtyInstance

        ts = build_triggerset()
        table = DirtyInstance._meta.db_table
        for name, trigger in ts.triggers.items():
            if trigger.db_table.startswith("test_app_profile"):
                for action in trigger.actions:
                    sql, _ = action.sql()
                    if table in sql:
                        assert "func_name" in sql, (
                            f"Trigger {name} inserts a DirtyInstance row "
                            "without func_name — that's a NULL whole-object "
                            "marker; library triggers must emit per-function "
                            "markers only."
                        )
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_depend_on_fields.py -v -k "PerFunction or TriggerSetShape"`
Expected: failures — bulk update still produces a NULL marker (`None in func_names`), trigger-set shape shows the catch-all (`columns` without `func_name`).

- [ ] **Step 3: Rewrite `CallbackDenorm.get_triggers`**

In `denorm/denorms.py`, replace the entire `CallbackDenorm.get_triggers` method (lines 200-248, the one inserting `columns=("content_type_id", "object_id")` NULL markers):

```python
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
        from .dependencies import DependOnFields
        from .models import DirtyInstance

        action = triggers.TriggerActionInsert(
            model=DirtyInstance,
            columns=("content_type_id", "object_id", "func_name"),
            values=(
                content_type,
                "NEW.%s" % qn(self.model._meta.pk.get_attname_column()[1]),
                # func.__name__ is a Python identifier — safe to inline.
                "'%s'" % self.func.__name__,
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
            # Undeclared function: conservative watch-all UPDATE trigger —
            # fires when any watched column changes (same conditions as the
            # old catch-all), addressed to this function. Declared functions
            # get their targeted UPDATE trigger from DependOnFields via
            # super().get_triggers().
            trigger_list.append(
                triggers.Trigger(
                    self.model,
                    "after",
                    "update",
                    [action],
                    content_type,
                    using,
                    self.skip,
                    self.only,
                    self.func,
                )
            )

        return trigger_list + super().get_triggers(using=using)
```

- [ ] **Step 4: Run the new tests**

Run: `pytest tests/test_depend_on_fields.py -v`
Expected: all pass. The cascade test exercises the Task 2 fix — if `letterhead` markers go missing mid-flush, re-check Task 2 landed.

- [ ] **Step 5: Run both full suites and triage**

Run: `pytest tests/ -q` and `python runtests.py postgres`.

Flush *semantics* are preserved (every column change still marks every affected function; flush recomputes the same functions), but two classes of existing assertions may legitimately change:

1. Tests asserting `func_name__isnull=True` markers from self-triggers — find them with `grep -n "func_name" test_denorm_project/test_app/tests.py tests/*.py`. Such assertions must now expect the function's name (e.g. `'spam'`/`'ham'`) instead of `None`. Update the assertion, not the behavior.
2. Tests counting DirtyInstance rows after a save — a model with N denorm fields now produces up to N per-function markers where it produced 1 NULL marker (dedup caps repeats). Adjust expected counts.

Any other failure mode (wrong values after flush, infinite flush loop) is a real bug in Step 3 — stop and fix before proceeding.

- [ ] **Step 6: Commit**

```bash
git add denorm/denorms.py tests/test_depend_on_fields.py test_denorm_project/ tests/
git commit -m "feat: per-function self-triggers; library no longer emits NULL markers"
```

---

### Task 6: `mark_dirty()` and the NULL-precedence contract

**Files:**
- Modify: `denorm/denorms.py` (add `mark_dirty` near `rebuild_instances_of`)
- Modify: `denorm/__init__.py` (export)
- Modify: `tests/test_depend_on_fields.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_depend_on_fields.py`:

```python
class TestMarkDirtyAndNullContract:
    def test_mark_dirty_emits_null_marker_and_flush_full_saves(
        self, transactional_db, denorm_triggers
    ):
        from test_app.models import Profile

        import denorm
        from denorm import denorms
        from denorm.models import DirtyInstance

        p = Profile.objects.create(first_name="John", last_name="Doe")
        denorms.flush()
        DirtyInstance.objects.all().delete()

        # Corrupt both denorm columns without leaving markers behind.
        Profile.objects.filter(pk=p.pk).update(letterhead="BROKEN")
        DirtyInstance.objects.all().delete()

        denorm.mark_dirty(p)
        marker = _markers(Profile, p.pk).get()
        assert marker.func_name is None

        denorms.flush()
        p.refresh_from_db()
        assert p.full_name == "John Doe"
        assert p.letterhead == "Dear John Doe"

    def test_mark_dirty_is_idempotent(self, transactional_db, denorm_triggers):
        from test_app.models import Profile

        import denorm
        from denorm.models import DirtyInstance

        p = Profile.objects.create(first_name="A", last_name="B")
        DirtyInstance.objects.all().delete()
        denorm.mark_dirty(p)
        denorm.mark_dirty(p)  # unique index + ignore_conflicts: no dupe, no error
        assert _markers(Profile, p.pk).count() == 1

    def test_null_takes_precedence_over_field_markers(
        self, transactional_db, denorm_triggers
    ):
        from unittest.mock import patch

        from test_app.models import Profile

        from denorm import denorms
        from denorm.models import DirtyInstance

        p = Profile.objects.create(first_name="John", last_name="Doe")
        denorms.flush()
        DirtyInstance.objects.all().delete()

        ct = ContentType.objects.get_for_model(Profile)
        DirtyInstance.objects.create(
            content_type=ct, object_id=p.pk, func_name="full_name"
        )
        DirtyInstance.objects.create(content_type=ct, object_id=p.pk)  # NULL

        seen = {}
        orig_save = Profile.save

        def spying_save(self, *args, **kwargs):
            seen["kwargs"] = kwargs
            return orig_save(self, *args, **kwargs)

        with patch.object(Profile, "save", spying_save):
            denorms.flush_single(ct.pk, p.pk, ct)

        assert "update_fields" not in seen["kwargs"], (
            "A func_name=NULL marker must force a FULL save (whole-object "
            "recompute), taking precedence over coexisting field markers."
        )

    def test_unknown_func_name_falls_back_to_full_save(
        self, transactional_db, denorm_triggers
    ):
        from test_app.models import Profile

        from denorm import denorms
        from denorm.models import DirtyInstance

        p = Profile.objects.create(first_name="John", last_name="Doe")
        denorms.flush()
        DirtyInstance.objects.all().delete()

        ct = ContentType.objects.get_for_model(Profile)
        # e.g. a marker from a field that was removed in a later deploy
        DirtyInstance.objects.create(
            content_type=ct, object_id=p.pk, func_name="removed_in_v2"
        )

        denorms.flush_single(ct.pk, p.pk, ct)  # must not raise
        assert not DirtyInstance.objects.exists()
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_depend_on_fields.py -k MarkDirty -v`
Expected: FAIL with `AttributeError: module 'denorm' has no attribute 'mark_dirty'`. (The two precedence/fallback tests pin existing behavior and should already pass — keep them anyway; they are the contract.)

- [ ] **Step 3: Implement `mark_dirty`**

In `denorm/denorms.py`, after `rebuild_instances_of`:

```python
def mark_dirty(*instances):
    """Explicitly mark whole objects dirty.

    Creates func_name=NULL markers — the only NULL markers the library
    produces besides rebuild_instances_of(). NULL means "recompute every
    denormalized field of this object" and takes precedence over
    field-level markers in flush_single.
    """
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
```

In `denorm/__init__.py` extend the denorms import and `__all__`:

```python
from .denorms import flush, mark_dirty, rebuildall
```

and add `"mark_dirty",` to `__all__`.

- [ ] **Step 4: Run the tests**

Run: `pytest tests/test_depend_on_fields.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add denorm/denorms.py denorm/__init__.py tests/test_depend_on_fields.py
git commit -m "feat: denorm.mark_dirty() explicit whole-object markers; pin NULL precedence"
```

---

### Task 7: AST scanner as Django system checks (E001/E002/W001/W002)

**Files:**
- Create: `denorm/checks.py`
- Modify: `denorm/apps.py`
- Create: `tests/test_checks.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_checks.py`:

```python
"""Tests for the @depend_on_fields declaration scanner (denorm/checks.py)."""

from __future__ import annotations


def _audit(model, fieldname):
    """Run the scanner for one denorm field of a real model."""
    from denorm.checks import audit_denorm
    from denorm.denorms import get_alldenorms

    for d in get_alldenorms():
        if d.model is model and d.fieldname == fieldname:
            return list(audit_denorm(d))
    raise AssertionError(f"no denorm {model.__name__}.{fieldname}")


class TestScanCallable:
    def test_collects_self_attribute_reads(self):
        from denorm.checks import scan_callable

        def func(self):
            return f"{self.first_name} {self.last_name}"

        reads, called, uncertain = scan_callable(func)
        assert reads == {"first_name", "last_name"}
        assert not called
        assert not uncertain

    def test_detects_dynamic_getattr_as_uncertain(self):
        from denorm.checks import scan_callable

        def func(self):
            return getattr(self, "first" + "_name")

        _, _, uncertain = scan_callable(func)
        assert uncertain

    def test_collects_self_method_calls(self):
        from denorm.checks import scan_callable

        def func(self):
            return self._compute_things()

        _, called, _ = scan_callable(func)
        assert called == {"_compute_things"}


class TestAuditDenorm:
    def test_complete_declaration_is_silent(self, db):
        from test_app.models import Profile

        assert _audit(Profile, "full_name") == []
        assert _audit(Profile, "letterhead") == []

    def test_undeclared_sibling_reads_warn_w001(self, db):
        from test_app.models import UndeclaredProfile

        messages = _audit(UndeclaredProfile, "full_name")
        assert [m.id for m in messages] == ["denorm.W001"]
        assert "first_name" in messages[0].msg
        assert "last_name" in messages[0].msg

    def test_incomplete_declaration_errors_e001(self, db):
        # Synthetic: a declared function that also reads an undeclared
        # column. Built without registering a model, by monkeypatching a
        # copy of the real denorm's pieces.
        from test_app.models import Profile

        from denorm.checks import audit_denorm
        from denorm.denorms import get_alldenorms

        d = next(
            x
            for x in get_alldenorms()
            if x.model is Profile and x.fieldname == "full_name"
        )

        def cheating_full_name(self):
            return f"{self.first_name} {self.last_name} {self.nickname}"

        cheating_full_name.__name__ = "full_name"

        class FakeDenorm:
            model = d.model
            fieldname = d.fieldname
            func = staticmethod(cheating_full_name)
            depend = d.depend  # declares only first_name, last_name

        messages = list(audit_denorm(FakeDenorm))
        assert [m.id for m in messages] == ["denorm.E001"]
        assert "nickname" in messages[0].msg

    def test_bad_declared_name_errors_e002(self, db):
        from test_app.models import Profile

        from denorm.checks import audit_denorm
        from denorm.dependencies import DependOnFields
        from denorm.denorms import get_alldenorms

        d = next(
            x
            for x in get_alldenorms()
            if x.model is Profile and x.fieldname == "full_name"
        )

        bad_dep = DependOnFields("no_such_column", func=d.func)
        bad_dep.setup(Profile)

        class FakeDenorm:
            model = d.model
            fieldname = d.fieldname
            func = staticmethod(d.func)
            depend = [bad_dep]

        ids = [m.id for m in audit_denorm(FakeDenorm)]
        assert "denorm.E002" in ids

    def test_dynamic_access_in_declared_func_warns_w002(self, db):
        from test_app.models import Profile

        from denorm.checks import audit_denorm
        from denorm.denorms import get_alldenorms

        d = next(
            x
            for x in get_alldenorms()
            if x.model is Profile and x.fieldname == "full_name"
        )

        def dynamic_full_name(self):
            return getattr(self, "first" + "_name")

        dynamic_full_name.__name__ = "full_name"

        class FakeDenorm:
            model = d.model
            fieldname = d.fieldname
            func = staticmethod(dynamic_full_name)
            depend = d.depend

        ids = [m.id for m in audit_denorm(FakeDenorm)]
        assert "denorm.W002" in ids


class TestRegisteredCheck:
    def test_registered_check_runs_and_test_models_have_no_errors(self, db):
        from denorm.checks import check_depend_on_fields

        messages = check_depend_on_fields(app_configs=None)
        errors = [m for m in messages if m.id.startswith("denorm.E")]
        assert errors == [], f"test models must stay declaration-honest: {errors}"
        # Member.full_name reads first_name/name without declarations —
        # the nudge warning must fire for it.
        w001_objects = {m.obj for m in messages if m.id == "denorm.W001"}
        assert "Member.full_name" in w001_objects
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_checks.py -v`
Expected: ERROR — `ModuleNotFoundError: No module named 'denorm.checks'`.

- [ ] **Step 3: Implement the scanner**

Create `denorm/checks.py`:

```python
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
        if name in (fieldname,) or _normalize_to_attnames(model, {name}) <= own:
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

    sibling_reads = _normalize_to_attnames(
        model, {r for r in reads if r in known_names}
    ) - own

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
```

In `denorm/apps.py`, register by importing inside `ready()`:

```python
class DenormAppConfig(AppConfig):
    name = "denorm"

    def ready(self):
        from denorm import checks  # noqa: F401 — registers system checks

        if getattr(settings, "DENORM_INSTALL_TRIGGERS_AFTER_MIGRATE", True):
            post_migrate.connect(denorm_install_triggers_after_migrate, sender=self)
```

- [ ] **Step 4: Run the tests**

Run: `pytest tests/test_checks.py -v`
Expected: all pass. If `test_registered_check_runs...` reports E001 for an existing test-app model, the scanner found a *genuinely* incomplete declaration in `Profile`/`ProfileReversed` — fix the model declaration, not the scanner.

- [ ] **Step 5: Run both full suites (checks now run inside manage.py commands)**

Run: `pytest tests/ -q` and `python runtests.py postgres`.
Expected: pass. W001 warnings for legacy test models (e.g. `Member.full_name`, `DenormModel.ham`) are expected console noise, not failures. If any command aborts with `denorm.E00x`, a test model violates its declarations — fix the model.

- [ ] **Step 6: Commit**

```bash
git add denorm/checks.py denorm/apps.py tests/test_checks.py
git commit -m "feat: system checks auditing @depend_on_fields declarations (E001/E002/W001/W002)"
```

---

### Task 8: Documentation, changelog, lint, final sweep

**Files:**
- Modify: `docs/reference.rst`
- Modify: `HISTORY.rst`

- [ ] **Step 1: Document the feature in `docs/reference.rst`**

Read the file first to match its heading style, then append a section:

```rst
Same-model dependencies: ``depend_on_fields``
---------------------------------------------

.. function:: denorm.depend_on_fields(*field_names)

   Declares that a :func:`denormalized` function reads the given sibling
   columns of its own model (plain columns or other denormalized fields)::

       class Person(models.Model):
           first_name = models.CharField(max_length=50)
           last_name = models.CharField(max_length=50)

           @denormalized(models.CharField, max_length=101)
           @depend_on_fields("first_name", "last_name")
           def full_name(self):
               return f"{self.first_name} {self.last_name}"

           @denormalized(models.CharField, max_length=120)
           @depend_on_fields("full_name")
           def letterhead(self):
               return f"Dear {self.full_name}"

   A declared function is marked dirty only when a declared column
   changes. An undeclared function keeps conservative semantics: any
   watched column change marks it dirty. ``@depend_on_fields()`` with no
   arguments means "reads no sibling columns".

   Declarations are audited by Django system checks (``denorm.E001``,
   ``denorm.E002``, ``denorm.W001``, ``denorm.W002``); an incomplete
   declaration means silent staleness, exactly like a missing
   :func:`depend_on_related`.

.. function:: denorm.mark_dirty(*instances)

   Explicitly marks whole objects dirty (``func_name=NULL`` markers).
   NULL means "recompute every denormalized field of this object" and
   takes precedence over field-level markers during flush. The library's
   own triggers never emit NULL; only ``mark_dirty`` and
   ``rebuildall``/``rebuild_instances_of`` do.

   After upgrading, run ``manage.py denorm_rebuild_triggers`` — trigger
   SQL changed shape (per-function markers).
```

- [ ] **Step 2: Add the `HISTORY.rst` entry**

Prepend under the newest-version heading, following the existing format (look at the 1.11.x entries for style):

```rst
1.12.0 (unreleased)
-------------------

* **Fix:** ``flush_single`` deletes claimed ``DirtyInstance`` markers at
  claim time. Previously the unique-index dedup silently swallowed any
  identical marker inserted mid-flush (by the flush's own triggers or a
  concurrent writer), permanently losing invalidations.
* **Feature:** ``@depend_on_fields`` — declarative same-model
  dependencies. Self-triggers are now per-function and targeted; the
  library no longer emits ``func_name=NULL`` (whole-object) markers from
  triggers. NULL is explicit-only (``denorm.mark_dirty()``, rebuild) and
  takes precedence over field markers during flush.
* **Feature:** ``denorm.mark_dirty(*instances)``.
* **Feature:** system checks ``denorm.E001/E002/W001/W002`` audit
  ``@depend_on_fields`` declarations (AST scanner).
* **Upgrade note:** run ``manage.py denorm_rebuild_triggers`` after
  upgrading. Targeted flush saves use ``update_fields`` and no longer
  touch ``auto_now`` columns; ``DENORM_DISABLE_AUTOTIME_DURING_FLUSH``
  now only matters for explicit whole-object (NULL) flushes.
```

- [ ] **Step 3: Lint and full suites**

Run: `flake8 denorm/ tests/` — Expected: clean.
Run: `pytest tests/ -q` — Expected: pass.
Run: `python runtests.py postgres` — Expected: pass.

- [ ] **Step 4: Commit**

```bash
git add docs/reference.rst HISTORY.rst
git commit -m "docs: document @depend_on_fields, mark_dirty, NULL contract; 1.12.0 changelog"
```

---

## Self-review notes (spec coverage)

- Design §Public API → Tasks 3 (decorator, empty declaration, stacking via union of per-decorator triggers), 6 (`mark_dirty`, NULL precedence pinned).
- Design §Trigger generation → Task 5 (per-function UPDATE, merged INSERT, conservative fallback, no NULL emission — asserted by `test_no_self_trigger_inserts_null_func_name`); declared-trigger shape → Task 3 unit tests.
- Design §Flush path → Tasks 1-2 (1.5 prerequisite), Task 6 (precedence + unknown-func fallback pins), Task 5 cascade test.
- Design §Scanner → Task 7 (E001/E002/W001/W002, helper-follow, dynamic-access uncertainty, registered check E2E).
- Design §Testing items 1-10 → Task 5 (items 1-3, 7, 8), Task 6 (items 4-6), Task 7 (item 9), Task 8 (item 10).
- Design §Sequencing → Task order; release/bumpver intentionally NOT in this plan (separate release step).
