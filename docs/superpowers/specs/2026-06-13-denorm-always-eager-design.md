# Design: `DENORM_ALWAYS_EAGER` — synchronous flush for tests

Status: draft for review
Target: 1.12.0 (last of the queue/flush batch)

## Problem / motivation

denorm is deferred by design: DB triggers mark objects dirty, `flush()`
recomputes later (inline, middleware, or the Celery queue). In **test
suites** that means a developer must sprinkle `denorm.flush()` after writes,
or denormalized fields on *dependent* objects stay stale — and a forgotten
flush is a silent test bug.

`DENORM_ALWAYS_EAGER` mirrors Celery's `task_always_eager`: when enabled,
denorm flushes synchronously after each write so denorm fields — including
those on dependent objects — are correct immediately, with no manual
`flush()` and no Celery worker.

**Primary audience: downstream projects' test suites.** denorm's *own* tests
of deferred / queue behavior keep it OFF and control flush timing
explicitly. The feature changes *timing* semantics (synchronous vs deferred),
so it is test-only; production keeps the deferred model.

A `Model.save()` already recomputes the saved object's *own* denorm fields
via `pre_save`. What eager adds is automatically flushing the **dependent**
markers (and same-model chains) that triggers enqueue — i.e. auto-running the
`flush()` that a test would otherwise call by hand.

## Decision: always-connected handler, setting read live

The signal handler is **always connected** in `AppConfig.ready()` (for every
project that installs denorm), and checks the setting **at call time**:

```python
# denorm/eager.py
import threading
from django.conf import settings
from django.db.models.signals import post_save, post_delete, m2m_changed

_state = threading.local()


def _eager_flush(sender, **kwargs):
    if not getattr(settings, "DENORM_ALWAYS_EAGER", False):
        return
    if getattr(_state, "flushing", False):
        return  # re-entrancy: flush() saves objects, which re-fire post_save
    _state.flushing = True
    try:
        from denorm import denorms
        denorms.flush()
    finally:
        _state.flushing = False


def connect():
    post_save.connect(_eager_flush, dispatch_uid="denorm_eager")
    post_delete.connect(_eager_flush, dispatch_uid="denorm_eager")
    m2m_changed.connect(_eager_flush, dispatch_uid="denorm_eager")
```

Wired in `denorm/apps.py` `ready()`: `from denorm import eager; eager.connect()`.

Rationale for "always connected, check live":

* **Togglable via `@override_settings(DENORM_ALWAYS_EAGER=True)`.** Connection
  happens once at app load; if it were gated on the setting *at connect
  time*, `override_settings` (which can't re-run `ready()`) couldn't turn it
  on in a test. Reading the setting **live** in the handler is what makes the
  standard Django test toggle work — the key usability property.
* **Negligible cost when off:** one `getattr` per `post_save`/`post_delete`/
  `m2m_changed`, returning immediately. No behavior change for the default.
* The handler reads `django.conf.settings` directly (not
  `denorm.conf.settings`, which snapshots the default at import and wouldn't
  see `override_settings`). `denorm/conf/settings.py` still gains a
  documented `DENORM_ALWAYS_EAGER` default for discoverability.

## Re-entrancy

`flush()` saves recomputed objects → those `post_save`s re-enter
`_eager_flush`. A thread-local `flushing` flag makes the re-entrant calls
no-ops; the outer `flush()` already drains everything to convergence
(including same-model chains via the convergence loop and dependent objects
via its outer loop). Thread-local (not a plain global) so parallel test
workers / threads don't disable each other's eager flush.

## What fires it, and marker visibility

* `post_save` (covers `Model.save()` / `create()`), `post_delete`,
  `m2m_changed` (m2m edits that dirty denorms).
* DB triggers insert markers **during** the write's SQL statement (same
  connection); `post_save` fires after, so `flush()` sees them. Under a
  `TestCase`'s wrapping transaction the markers and the flush share the
  connection — eager works without a commit, which is the point.
* `QuerySet.update()` / `bulk_create` / `bulk_update` fire **no** per-row
  signals, so eager does not auto-flush them — documented. (The self-trigger
  still marks them dirty; a later `flush()` or another signalled save settles
  them. This matches the deferred model and is acceptable for the test
  ergonomic.)
* `mark_dirty()` uses `bulk_create` (no signal) → not auto-flushed by eager
  on its own; callers pair it with a save or an explicit flush. Documented.

## Scope / non-goals

* **Not** `transaction.on_commit`: those callbacks do **not** fire under a
  `TestCase`'s rolled-back transaction, so they can't serve the test
  use-case. (This was the on-commit-flusher idea rejected earlier; eager is
  its test-only realization.)
* **Not** for production: it reintroduces synchronous coupling (a bulk write
  dirtying many dependents would flush them all inline). Documented as
  test-only; default off.

## Tests

PostgreSQL functional tests (testcontainers), in `tests/`:

1. **Dependent denorm correct without manual flush.** With
   `@override_settings(DENORM_ALWAYS_EAGER=True)`: a write that dirties a
   *dependent* object (e.g. change a Forum-related row so a `Post` denorm
   goes stale, or the Profile chain) leaves the dependent's denorm field
   correct immediately, with **no** `denorm.flush()` call.
2. **Off by default.** Without the setting, the same write leaves the
   dependent stale until an explicit `flush()` — proves opt-in and that the
   handler is a no-op when off.
3. **Same-model chain settles eagerly.** `full_name` → `letterhead`: an ORM
   save with eager on yields both correct without manual flush (exercises
   re-entrancy + convergence under eager).
4. **No infinite recursion / hang.** The chain/dependent tests must complete
   (the thread-local guard works); a deliberate assertion that `flush()` is
   entered once per triggering save, not unboundedly.
5. **m2m edit** with eager on settles the m2m-dependent denorm without manual
   flush (if a test model exposes an m2m denorm; otherwise note coverage).
6. Full suites green with eager OFF (default) — no regression to existing
   behavior.

## Settings / docs

* `denorm/conf/settings.py`: `DENORM_ALWAYS_EAGER = getattr(settings,
  "DENORM_ALWAYS_EAGER", False)` with a test-only comment.
* `docs/reference.rst`: document the setting, the CELERY_ALWAYS_EAGER
  analogy, the test-only framing, and the `QuerySet.update()`/bulk caveat.
* `HISTORY.rst`: 1.12.0 entry. No migration, no trigger change.

## Files

- Create `denorm/eager.py` (handler + `connect()`).
- Modify `denorm/apps.py` (`ready()` calls `eager.connect()`).
- Modify `denorm/conf/settings.py` (default).
- Create `tests/test_eager.py`.
- Modify `docs/reference.rst`, `HISTORY.rst`.
