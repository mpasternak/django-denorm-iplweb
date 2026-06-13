# Design: flush convergence loop (audit spec 2.5)

Status: draft for review
Target: a 1.12.x / 1.13.0 performance change
Supersedes: the stale sketch in `docs/spec-concurrency-performance-fixes.md` §2.5
(written before per-function markers; its "markers are func_name=NULL → next
pass = full save" assumption no longer holds).

## Problem

`flush()` runs an **outer loop**: scan all distinct
`(content_type_id, object_id)` pairs in `denorm_dirtyinstance`, call
`flush_single` for each, and repeat the whole scan until a pass processes
zero pairs.

`flush_single(ct, oid)` runs in one transaction: lock the object,
claim-and-delete its markers (spec 1.5), recompute and `save()`.

When a `save()` changes a stored value, its UPDATE fires denorm triggers that
insert **new** markers. For a same-model chain — e.g. `Profile.letterhead`
`@depend_on_fields("full_name")` — changing the `full_name` column fires
`letterhead`'s trigger, inserting `(ct, oid, 'letterhead')` for the **same**
object. That marker is created *after* `flush_single` already claimed-and-
deleted, so it survives (spec 1.5 freed the key) and `flush_single` returns
with it still in the table.

The outer loop then runs a **second full pass** to process it: a second
transaction, a second object lock, a second whole-table distinct scan. A
chain of depth N costs N outer passes per object.

This is **pure overhead** — the outer loop already converges to a correct
result. The cost is transaction/lock/scan churn on the hot path.

## Design

Converge inside `flush_single`'s existing transaction. The markers the
`save()`'s triggers insert are created in **our own transaction** and are
visible to us before commit (the same MVCC fact spec 1.5 relies on), so we
process them immediately instead of returning:

```python
@retry_on_serialization_failure
def flush_single(content_type_id, object_id, content_type=None):
    ...
    with transaction.atomic():
        obj = lock_object_or_bail()              # existing lock + bail paths
        scope = _claim_and_delete_markers(ct, oid)   # see return-shape note
        if not scope:
            return

        for _ in range(settings.DENORM_MAX_CONVERGE_PASSES):   # e.g. 5
            obj.save(**_build_save_kwargs(scope))
            scope = _claim_and_delete_markers(ct, oid)   # markers our own save
            if not scope:                                # inserted are visible
                break                                    # in-tx (MVCC)
        # markers beyond the cap (if any) stay for the outer loop /
        # the DENORM_MAX_FLUSH_PASSES valve
```

Illustrative names: only `_claim_and_delete_markers` exists today.
`lock_object_or_bail()` denotes the existing object-lock plus the
`DoesNotExist` / locked-elsewhere bail logic; `_build_save_kwargs()` is the
existing `update_fields` logic factored out of `flush_single` (NULL in
`scope` → full save; otherwise `update_fields=[validated func names]`, minus
auto_now exclusions).

**Return-shape refactor (consistency fix found in self-review).** Today
`_claim_and_delete_markers` returns a `(claimed_any, func_names)` tuple, so
the `scope = ...; if not scope` form above would be wrong (a tuple is always
truthy). Change the helper to return **just the `func_names` set** (empty set
when nothing is claimable); `if not scope` then replaces the `claimed_any`
flag, and `scope` everywhere is that set (NULL membership → full save). The
deleted-object bail path already ignores the return value, so it is
unaffected.

### Per-function markers (this is what changed since the original §2.5)

After the `@depend_on_fields` work the library emits **per-function**
markers and never `func_name=NULL` from triggers. So the markers re-claimed
by the loop are **targeted** (`'letterhead'`), and each iteration does a
targeted `update_fields` save — *better* than the original sketch's
"full save". An explicit `mark_dirty()` NULL marker for the same object
arriving mid-loop still forces a full save on the iteration that claims it
(NULL precedence, unchanged).

### Scope discipline (critical)

The loop re-claims markers **only for this `(ct, oid)`**. Cascade markers for
**other** objects (e.g. changing this object dirties a *related* object's
denorm) are a different `(ct, oid)` — they must be **left** for the normal
flush path (outer loop inline, or a new `flush_via_queue` round in the queue
path). `_claim_and_delete_markers(ct, oid)` already filters to the single
pair via `_markers_for`, so this falls out for free — but it is a load-
bearing invariant, not an incidental one.

### Own-column exclusion interaction

A function's conservative self-trigger no longer watches its own column (the
own-column exclusion shipped with per-function triggers), so a function
cannot re-mark itself. The markers the loop sees are therefore genuine
*downstream* chain markers (`full_name` save → `letterhead` marker), not
self-noise. This is what guarantees the loop shrinks the work each iteration
rather than spinning on one field.

### Termination

Each save recomputes from the values just written. Once the chain is stable,
every trigger's `IS DISTINCT FROM` (now in the `WHEN` clause) is false → no
marker inserted → `scope` empty → loop exits. `DENORM_MAX_CONVERGE_PASSES`
(new setting, default 5) caps non-deterministic denorm functions; on hitting
the cap, remaining markers stay in the table and are handled by the outer
loop, ultimately bounded by `DENORM_MAX_FLUSH_PASSES`. Degraded behavior
equals today's behavior.

A *legitimate* same-model chain deeper than the cap is also handled
correctly — the overflow links simply fall back to outer-loop passes (no
infinite loop, just less optimization). Set the default comfortably above
realistic chain depth.

### Concurrency safety (identical argument to spec 1.5)

* Markers from concurrent **uncommitted** transactions are invisible — left
  for later.
* Markers from concurrent **committed** transactions may be visible to a
  re-claim (READ COMMITTED takes a fresh snapshot per statement). Claiming
  them is correct: the recompute that follows reads the same committed state
  that produced them.
* A concurrent marker insert colliding with a key we claimed waits on our
  uncommitted delete and lands after our commit (spec 1.5) — never lost.
* All claims use `select_for_update(skip_locked=True)`; we never block on
  another flush worker.
* A serialization failure (40001 / 40P01) mid-loop rolls back the whole
  transaction; `retry_on_serialization_failure` re-runs `flush_single` from
  scratch (re-lock, re-claim, re-loop). It is idempotent — no partial state
  survives a rolled-back attempt.

**Reusing the in-memory `obj` across iterations is safe — and this needs
spelling out (self-review gap).** We hold `select_for_update(of=('self',))`
on the object row for the whole transaction, so no concurrent transaction can
change *this object's own source columns* under us. A chain marker for one of
its own denorm fields therefore cannot arise from a concurrent write during
the loop — and the own-column exclusion means our own saves don't produce one
either. The markers the loop re-claims are thus one of:

* **(a) downstream same-object chain links** (`full_name` save →
  `letterhead` marker) — these recompute correctly from the in-memory denorm
  fields that prior iterations' `pre_save` already refreshed on `obj`; or
* **(b) dependency-driven markers** whose denorm function reads *related*
  objects — `pre_save` recomputes these with a fresh query, not from `obj`'s
  in-memory state.

Neither case reads stale in-memory *source* data, so the loop does not need a
`refresh_from_db()` between iterations. (Without this argument the reuse would
look unsafe; it is the object-row lock + own-column exclusion that make it
sound.)

The loop holds the object row lock marginally longer (one extra UPDATE per
chain link). That is the deliberate trade: fewer transactions/locks/scans
for slightly longer single-object lock duration.

## NOTIFY interaction (deliberate decision, not an oversight)

The `AFTER INSERT ON denorm_dirtyinstance FOR EACH STATEMENT` trigger
(migration 0016) emits one `pg_notify` per marker-insert statement,
delivered on COMMIT. The convergence loop consumes same-object chain markers
in-transaction, but their INSERT still executed, so their NOTIFY **still
fires at commit** and wakes `denorm_queue` to schedule a flush that finds
nothing for that object — a **wasted wakeup**.

This is accepted, for two reasons:

1. The loop does not change the *number* of NOTIFYs (it cannot prevent the
   chain trigger from inserting); it only makes a fraction of the resulting
   wakeups no-ops. `flush_via_queue` is `Singleton`-deduped, so a burst of
   NOTIFYs in one commit collapses to one flush task regardless.
2. NOTIFY-during-flush is **load-bearing in the queue path**:
   `flush_via_queue` snapshots distinct pairs once and dispatches
   `flush_batch`; cross-object cascade markers created during processing are
   not in that snapshot and are rediscovered **only** via the NOTIFY their
   insert fires. Blanket-suppressing flush-internal NOTIFY would orphan
   cascades. Reducing NOTIFY volume safely is therefore a **separate,
   larger** change (see Future note) — out of scope here.

So: the convergence loop ships accepting the bounded wasted wakeups; NOTIFY
volume reduction is tracked separately.

## Tests

PostgreSQL functional tests (testcontainers), in `tests/`:

1. **One transaction for a chain.** Dirty `Profile.full_name` via its source
   column; assert a single `flush_single(ct, oid)` call leaves
   `denorm_dirtyinstance` empty for that object and both `full_name` and
   `letterhead` correct (today this needs two `flush_single` calls / two
   outer passes). Spy on `Profile.save` to assert it was called exactly
   twice within one `flush_single` (one per chain link), not across two
   transactions.
2. **Scope discipline.** A change that dirties a *related* object: assert the
   convergence loop does NOT consume the related object's marker (it remains
   for the outer loop), while the same-object chain is consumed in-tx.
3. **Non-deterministic function hits the cap.** A denorm function returning a
   changing value: assert the loop stops at `DENORM_MAX_CONVERGE_PASSES`,
   leaves a marker, and does not hang (the `DENORM_MAX_FLUSH_PASSES` valve
   still bounds the outer loop).
4. **Concurrent committed marker.** Staged like the 1.5 tests: a marker
   committed by another actor mid-loop is either claimed-and-recomputed or
   left intact — never deleted without a recompute.
5. **NULL precedence mid-loop.** A `mark_dirty()` NULL marker for the same
   object claimed by a later iteration forces a full save.
6. Full existing suites stay green.

## Settings / docs

* New `DENORM_MAX_CONVERGE_PASSES` (default 5) in `denorm/conf/settings.py`.
* `docs/reference.rst`: document the setting and the convergence behavior.
* `HISTORY.rst`: 1.12.x/1.13.0 entry (pure performance; no API change, no
  migration; trigger SQL unchanged so no `denorm_rebuild_triggers` needed).
* Mark §2.5 in `docs/spec-concurrency-performance-fixes.md` shipped, pointing
  here.

## Future note (separate effort) — NOTIFY volume / queue self-sufficiency

The queue path currently relies on flush-internal NOTIFYs to rediscover
cross-object cascades. Two related improvements, neither in scope here:

* Make `flush_via_queue` **loop to convergence** (re-snapshot distinct pairs
  until a round dispatches nothing), making it self-sufficient — after which
  flush-internal NOTIFY can be suppressed entirely via a session GUC
  (`SET LOCAL denorm.flushing='on'`, checked by the 0016 trigger).
* Keep the LISTEN/NOTIFY bridge regardless: it is the right tool precisely
  because it is **transactional** (fires on commit, respects rollback) and
  needs no infrastructure beyond the listener process. Writing to Redis
  directly from a trigger breaks transactionality (phantom work on rollback);
  CDC / logical replication is robust but disproportionate infrastructure for
  this. The improvement space is *how much* we NOTIFY, not replacing the
  mechanism.

## Out of scope

* The NOTIFY-volume reduction above.
* Parallelizing the inline `flush()` (the queue path already parallelizes).
* Any change to marker shape or trigger SQL.
