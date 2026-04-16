# Django 6.0: `Field.pre_save()` called twice during INSERT

## Summary

Django 6.0 introduced a behavioral change where `Field.pre_save()` may be called
**more than once** during a single `Model.save()` operation. This affects all custom
fields that override `pre_save()` with non-idempotent logic.

**Django ticket**: https://code.djangoproject.com/ticket/36855
**Causal commit**: `94680437a45a71c70ca8bd2e68b72aa1e2eff337` (Simon Charette,
"Fixed #27222 -- Refreshed model field values assigned expressions on save()")

## What changed

### Django 4.2 / 5.2 — INSERT path

`Model._save_table()` passes `fields` directly to `_do_insert()`. The only place
`pre_save()` gets called is inside `SQLInsertCompiler.as_sql()` via `pre_save_val()`:

```
Model.save()
 └─ _save_table()
     └─ _do_insert(fields=meta.local_concrete_fields)
         └─ SQLInsertCompiler.as_sql()
             └─ pre_save_val(field, obj)
                 └─ field.pre_save(obj, add=True)    ← ONLY call
```

### Django 6.0 — INSERT path

`_save_table()` now iterates over `insert_fields` and calls `field.pre_save()` to
check whether the returned value has a `resolve_expression` attribute (for expression-
based field assignments). Then `_do_insert()` calls it again through the compiler:

```
Model.save()
 └─ _save_table()
     ├─ for field in insert_fields:
     │      field.pre_save(self, add=True)            ← FIRST call (new in 6.0)
     │      # checks hasattr(value, "resolve_expression")
     │
     └─ _do_insert(...)
         └─ SQLInsertCompiler.as_sql()
             └─ pre_save_val(field, obj)
                 └─ field.pre_save(obj, add=True)     ← SECOND call
```

The relevant lines in Django 6.0 `django/db/models/base.py`:

```python
# Line ~1158 — NEW in Django 6.0
for field in insert_fields:
    value = (
        getattr(self, field.attname)
        if raw
        else field.pre_save(self, add=True)   # ← first call
    )
    if hasattr(value, "resolve_expression"):
        ...

# Line ~1169
results = self._do_insert(...)               # ← triggers second call via compiler
```

### UPDATE path — unchanged

The UPDATE path in `_save_table()` calls `pre_save()` once and passes the values
directly to `_do_update()`. The compiler does NOT call `pre_save()` again for
updates. This behavior is the same across all Django versions.

## Django's official position

The Django team resolved ticket #36855 as a **documentation fix** (not a code fix).
The release notes for 6.0 now state that `Field.pre_save()` may be called more than
once during `Model.save()`, and custom fields must ensure their `pre_save()` is
idempotent and side-effect free.

**Documentation PR**: https://github.com/django/django/pull/20534

## Impact on django-denorm-iplweb

### Affected fields

All three `pre_save()` implementations in `denorm/fields.py` were affected:

1. **`DenormDBField.pre_save()`** (the `@denormalized` decorator) — calls the user's
   denorm function and sets the result on the instance. If the function reads the
   current field value and derives the new one from it (like `CallCounter` below),
   the second call sees the first call's result and computes a different value.

2. **`CacheKeyField.pre_save()`** — calls `random.randint()`. Two calls produce two
   different random values. The instance ends up with the first value, while the
   database gets the second — a subtle inconsistency.

3. **`AggregateField.pre_save()`** (`CountField`, `SumField`) — already idempotent.
   Returns 0 for `add=True`, reads from DB for updates. No fix needed.

### Concrete example: the `CallCounter` test model

```python
# test_app/models.py
class CallCounter(models.Model):
    @denormalized(models.IntegerField)
    def called_count(self):
        if not self.called_count:
            return 1
        return self.called_count + 1
```

**Django 4.2 / 5.2** — `CallCounter.objects.create()`:
- `pre_save()` called once: `called_count` is `None` → returns `1`
- Database value: `1`

**Django 6.0 (before fix)** — `CallCounter.objects.create()`:
- `pre_save()` called first time: `called_count` is `None` → returns `1`, sets `called_count=1` on instance
- `pre_save()` called second time: `called_count` is `1` → returns `2`
- Database value: `2` (one phantom increment)

### Our fix

We made `pre_save()` idempotent by caching the computed value within a save cycle:

```python
def pre_save(self, model_instance, add):
    cache_attr = f"_denorm_pre_save_{self.attname}"
    cached = getattr(model_instance, cache_attr, None)
    if cached is not None:
        return cached              # Return same value on repeated calls

    value = self.denorm.func(model_instance)
    setattr(model_instance, self.attname, value)
    setattr(model_instance, cache_attr, value)  # Cache for this save cycle
    return value
```

The cache is cleared after each `save()` via a `post_save` signal handler, so that
subsequent saves (e.g., from `denorm.flush()`) recompute the value fresh:

```python
def _clear_denorm_pre_save_cache(sender, instance, **kwargs):
    for attr in list(vars(instance)):
        if attr.startswith("_denorm_pre_save_"):
            delattr(instance, attr)

# Connected in contribute_to_class() with dispatch_uid to avoid duplicates
models.signals.post_save.connect(
    _clear_denorm_pre_save_cache,
    sender=cls,
    dispatch_uid=f"denorm_clear_pre_save_cache_{cls.__name__}",
)
```

## Guidance for users writing `@denormalized` functions

Your denorm function should compute the field value from the **current state of
related objects**, not from the field's own current value. This has always been
the intended pattern, but Django 6.0 makes it a hard requirement.

**Good** — idempotent, computes from related objects:
```python
@denormalized(models.IntegerField)
@depend_on_related('Comment')
def comment_count(self):
    return self.comment_set.count()
```

**Good** — idempotent, pure computation:
```python
@denormalized(models.CharField, max_length=255)
@depend_on_related('Author')
def author_names(self):
    return ", ".join(a.name for a in self.authors.all())
```

**Dangerous** — reads own value and derives from it:
```python
@denormalized(models.IntegerField)
def called_count(self):
    # This pattern is fragile: if pre_save() is called twice,
    # the count increments twice per save.
    if not self.called_count:
        return 1
    return self.called_count + 1
```

## Verification

All 38 tests pass on Django 4.2, 5.2, and 6.0, confirmed via testcontainers:

```bash
# Run tests against a disposable PostgreSQL container
.tox/py312-django60/bin/python run_tests_tc.py
```
