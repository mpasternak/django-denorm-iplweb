# Design: drop redundant self-markers on ORM saves (architecture #1)

Status: draft for review
Target: 1.12.0 (or 1.12.x follow-up)

## Problem

Every ordinary `Model.save()` of a denormed model leaves dirty markers even
though `Field.pre_save()` already computed the object's own denorm fields
correctly during that save. `flush()` then re-saves the object redundantly.
Demonstrated live (this session):

```
[after create()]  columns already correct via pre_save:
    full_name = 'John Doe'   letterhead = 'Dear John Doe'
    DirtyInstance markers anyway: ['full_name', 'letterhead']
[during flush()] redundant Profile.save(update_fields=['full_name','letterhead'])
```

The self-trigger fires on every insert (unconditional) and every update of a
watched column, because it **cannot tell an ORM save (pre_save already ran,
values fresh) from a `QuerySet.update()` / raw-SQL write (pre_save bypassed,
values stale)**. It conservatively marks dirty. That conservatism is correct
for the bypass case and wasteful for the ORM case.

Steady-state cost per ORM write: N marker INSERTs (trigger) + N marker
DELETEs (flush) + one redundant re-save UPDATE per written object.

## Approach

A `post_save` handler runs in Python, **after** the INSERT/UPDATE, **in the
save's transaction** — so it *knows* `pre_save` ran (this was an ORM save)
and can see the markers the trigger just created (same connection). It
deletes the markers that are **provably redundant**: those whose denorm
function this save recomputed to a value a fresh recompute would also yield.

Crucially this does **not** weaken bypass detection: `QuerySet.update()`,
`bulk_create`, `bulk_update`, raw SQL, and `mark_dirty` fire **no** per-row
`post_save`, so their markers are never touched by this handler and flush
still settles them. We only trim the ORM-save path — exactly the redundant
case.

## Safety boundary (the crux — deleting an invalidation marker wrongly =
permanent stale data)

Delete marker `(content_type, object_id, func)` **iff all** hold:

1. **`func` is a denormalized field of the saved model** (a real denorm
   func, not a foreign NULL / unknown name).
2. **`func` is self-contained on plain columns:** every name in its
   `@depend_on_fields` resolves to a **plain (non-denormalized) column** of
   the same model, **and** `func` has **no** `@depend_on_related` (and no
   CacheKey/aggregate dependency). I.e. `func`'s value is a pure function of
   this row's own non-denorm columns.
3. **`func` was recomputed by this save:** `update_fields is None` (full
   save → all denorm `pre_save`s ran) **or** `func`'s field name is in
   `update_fields`.

Delete **only** such markers, and **only** for this `(content_type,
object_id)`. Everything else is left for the normal (now convergent) flush.

### Why each condition is necessary (correctness proof per exclusion)

* **Plain-column same-model (kept) — SAFE.** `pre_save` computed `func` from
  this row's own non-denorm columns. Those columns hold the values just
  written (caller set them; the save wrote them); a fresh recompute reads the
  same columns → identical value. Order-independent (plain columns don't
  change across `pre_save` passes). Concurrency-safe: the save holds a row
  lock on its own row until commit, so no concurrent writer can change those
  source columns under us; a concurrent committed marker for the same `func`
  is satisfied because our full write made the row self-consistent
  (last-write-wins on the source column, which our save performed).

* **Chain deps (a `@depend_on_fields` name that is itself a denorm field) —
  EXCLUDED.** `pre_save` computed `func` from `self.<other_denorm>`, correct
  only if `<other_denorm>` was recomputed *before* `func` in the same save —
  which depends on **field definition order** (e.g. `ProfileReversed` defines
  `letterhead` before `full_name`, so `letterhead`'s `pre_save` reads a stale
  `full_name`). Not guaranteed → leave the marker; the `flush_single`
  convergence loop (spec 2.5) settles chains correctly regardless of order.

* **Related deps (`@depend_on_related`) — EXCLUDED.** `pre_save` computed
  `func` from in-memory related objects (`self.fk.x`), which may be **staler
  than the DB** (the related row changed after the caller loaded it; that
  change is exactly why a marker exists). The deferred flush re-loads the
  object (`select_for_update`) and recomputes from fresh DB state. Trusting
  `pre_save` here would lose the invalidation → stale. Leave the marker.

* **Undeclared funcs (no `@depend_on_fields`) — EXCLUDED.** We don't know
  what they read (could be related via `self.fk`). Conservative → leave.

* **Not recomputed (`func` not in a non-None `update_fields`) — EXCLUDED.**
  `pre_save` didn't run for `func`, so its column is stale while a dependency
  changed (e.g. `save(update_fields=['first_name'])` leaves `full_name`
  stale). The trigger's marker is real → leave it.

* **`func_name=NULL` markers — EXCLUDED.** Whole-object explicit dirty
  (`mark_dirty`/rebuild) — never ours to second-guess.

* **Other objects' markers — EXCLUDED.** A save can fire dependency triggers
  marking *other* objects (different `(ct, oid)`); we only ever delete this
  object's markers. (Filter to `(ct, self.pk)`.)

### Transaction safety
The handler runs inside the save's transaction. The trigger's marker INSERT
and the handler's DELETE are in the same transaction → net zero on commit (no
other transaction observes the marker), and on rollback both vanish — fully
consistent. Deleting uses a plain `DELETE ... WHERE content_type=… AND
object_id=… AND func_name IN (safe_funcs)`; no lock contention (we already
hold the row; markers are ours, just created).

## Where / how

A per-model `post_save` handler connected in
`DenormDBField.contribute_to_class` (and `CacheKeyField`/`CachedField` if
relevant) with `sender=cls` and a per-model `dispatch_uid` — mirroring the
existing `_clear_denorm_pre_save_cache` wiring (fields.py). The set of
"safe funcs" for a model is computed once (lazily/cached) from the model's
denorm fields and their `depend` lists:

```python
def _safe_self_funcs(model):
    """denorm field names whose value is a pure function of this row's own
    PLAIN columns (deletable in post_save). Cached on the model."""
    from denorm.dependencies import DependOnFields  # + Callback/CacheKey deps
    safe = set()
    for f in model._meta.fields:
        denorm = getattr(f, "denorm", None)
        if denorm is None or not getattr(denorm, "func", None):
            continue
        deps = getattr(denorm, "depend", [])
        depfields = [d for d in deps if isinstance(d, DependOnFields)]
        # must have at least one DependOnFields and NO other dep kind
        if not depfields or len(depfields) != len(deps):
            continue
        names = {n for d in depfields for n in d.field_names}
        # every declared dep must be a PLAIN (non-denorm) column
        if all(_is_plain_column(model, n) for n in names):
            safe.add(denorm.fieldname)
    return safe
```

`_is_plain_column(model, name)`: resolves `name` (field name or attname) to a
concrete field and checks it does NOT have a `denorm` attribute. (Exact
resolution mirrors `DependOnFields.resolve_attnames`.)

Handler:

```python
def _drop_redundant_self_markers(sender, instance, update_fields=None, **kwargs):
    safe = _safe_self_funcs(sender)
    if not safe:
        return
    if update_fields is not None:
        safe = safe & set(update_fields)
        if not safe:
            return
    from django.contrib.contenttypes.models import ContentType
    from denorm.models import DirtyInstance
    ct = ContentType.objects.get_for_model(sender)
    DirtyInstance.objects.filter(
        content_type=ct, object_id=instance.pk, func_name__in=safe
    ).delete()
```

Note: this only ever deletes `func_name IN safe` — NULL markers and
unknown/related/chain func markers are never matched. `update_fields`
intersection enforces condition 3.

### Interaction with other features
* **eager (`DENORM_ALWAYS_EAGER`):** both are `post_save` handlers; they
  compose. With eager off (default), this just trims redundant markers. With
  eager on, this trims the safe ones and eager's flush handles the rest;
  signal order doesn't affect correctness (either we delete then eager
  flushes the remainder, or eager flushes — redundantly re-saving — then we
  delete nothing). Correct both ways.
* **convergence loop (2.5):** untouched markers (chain/related) still flush
  convergent.
* **bulk-unsafe triggers (`DENORM_BULK_UNSAFE_TRIGGERS`):** no self-trigger
  at all → no self-markers to drop; handler is a no-op. Fine.

## Tests (PostgreSQL functional)

1. **Plain-column same-model marker dropped.** Save a Profile (full save):
   `full_name`'s marker is gone after the save (no manual flush), value
   correct. Spy `Profile.save` during a subsequent `flush()` → NOT re-saved
   for `full_name` (redundancy eliminated).
2. **Chain marker kept.** `letterhead` (depends on the denorm field
   `full_name`) marker REMAINS after save (safety: order-sensitive). A later
   `flush()` settles it correctly.
3. **Related-dep marker kept.** A model with `@depend_on_related`
   (`Post.forum_title`): its marker is never dropped by an ORM save of the
   Post; flush settles it.
4. **`update_fields` respected.** `profile.save(update_fields=['first_name'])`
   → `full_name` NOT recomputed → its trigger marker REMAINS (not dropped);
   full save → dropped.
5. **Bypass paths untouched.** `Profile.objects.filter(...).update(...)`
   (no post_save) → marker stays → flush still settles. (Proves bypass
   detection intact.)
6. **Concurrency.** Staged: a concurrent committed marker for a plain-column
   func + our full save → marker correctly dropped (row self-consistent); a
   concurrent committed RELATED-dep marker is NOT dropped (no stale loss).
7. **End-to-end correctness.** A scenario touching plain/chain/related denorms
   then a full `flush()` yields correct values everywhere (no stale) — the
   optimization changes performance, not results.

## Docs
* `docs/tutorial.rst` "Callbacks are lazy": note that for denorm fields that
  are a pure function of the row's own plain columns, an ORM `save()` already
  produces the correct value and the redundant marker is dropped in
  `post_save` (no flush re-save needed); chain/related denorms still go
  through flush.
* `docs/reference.rst`: brief note (no new setting).
* `HISTORY.rst`: 1.12.0 perf bullet.

## Out of scope / future
* **Chains via field-order analysis.** Could safely drop chain markers when
  every denorm dependency is defined *before* the dependent field (so
  `pre_save` order is correct). Fragile (relies on `Meta` field order);
  deferred. Convergence already makes chains cheap.
* **Related-dep markers.** Genuinely need the deferred fresh re-read; not
  optimizable this way.
* No new setting; the optimization is always on (it only ever removes
  provably-redundant markers and never changes results).
