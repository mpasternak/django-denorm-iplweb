# Spec: concurrency, reliability and performance fixes

Status: draft
Target version: 1.12.0
Scope: fixes identified during the June 2026 concurrency/performance audit.

Each item below states the problem, the change, affected files, tests, and
risks. Items are grouped into phases by urgency. Phases are independently
shippable; within a phase, items are independent unless noted.

---

## Phase 1 — reliability bugs (silent failures)

### 1.1 `denorm_queue` leaks memory: NOTIFY payloads never drained

**Problem.** `denorm/management/commands/denorm_queue.py` calls
`pg_con.poll()` after `select()`, which makes psycopg2 append every received
NOTIFY to `pg_con.notifies`. The list is never consumed. The statement-level
trigger (migration 0016) sends a NOTIFY on every INSERT statement into
`denorm_dirtyinstance`, so this long-running daemon grows without bound under
sustained write traffic.

**Change.** Clear the list after polling; we only care that *something*
arrived, the payload is empty anyway:

```python
pg_con.poll()
del pg_con.notifies[:]   # or pg_con.notifies.clear()
flush_via_queue.delay()
```

**Files.** `denorm/management/commands/denorm_queue.py` (`_listen_loop`).

**Tests.** Extend the existing run-once test: send several NOTIFYs, run the
loop, assert `pg_con.notifies` is empty afterwards.

**Risk.** None.

### 1.2 `denorm_queue` ignores pre-existing backlog on startup/reconnect

**Problem.** After `LISTEN` is established, `_listen_loop` blocks in
`select()` until a *new* NOTIFY arrives. PostgreSQL does not queue
notifications for disconnected listeners, so dirty rows that accumulated
while the listener was down (deploy, failover — exactly what the reconnect
logic in `handle()` exists for) sit unprocessed until the next unrelated
write.

**Change.** Fire one `flush_via_queue.delay()` immediately after the
`LISTEN` statement succeeds, before entering the select loop. The
`Singleton` base dedups if a flush is already queued.

**Files.** `denorm/management/commands/denorm_queue.py` (`_listen_loop`).

**Tests.** run-once test: create a DirtyInstance *before* starting the loop,
assert `flush_via_queue.delay` was called (mock) without any NOTIFY being
sent.

**Risk.** None; at worst one redundant no-op task per reconnect.

### 1.3 `celery_singleton` lock leak on worker crash

**Problem.** `denorm/tasks.py` uses `base=Singleton` with no lock expiry.
celery-singleton releases its Redis lock in `on_success`/`on_failure`; if a
worker dies hard (OOM-kill, SIGKILL, power loss) mid-task, the lock persists
forever and that `(content_type_id, object_id)` pair can never be enqueued
again. Denormalization for that object silently stops until someone clears
Redis by hand.

**Change.** Add a lock expiry to both tasks, with a setting:

```python
# denorm/conf/settings.py
DENORM_SINGLETON_LOCK_EXPIRY = getattr(
    settings, "DENORM_SINGLETON_LOCK_EXPIRY", 600
)  # seconds

# denorm/tasks.py
@shared_task(
    base=Singleton,
    ignore_result=False,
    lock_expiry=settings.DENORM_SINGLETON_LOCK_EXPIRY,
)
```

600 s default: comfortably above any sane single-object flush, short enough
that a crashed worker only pauses (not kills) processing of that object.

**Files.** `denorm/tasks.py`, `denorm/conf/settings.py`, docs.

**Tests.** Assert the task classes carry `lock_expiry`; document the setting
in `docs/reference.rst`.

**Risk.** A flush legitimately running longer than the expiry allows a
duplicate concurrent task. That is safe: `flush_single` is written to be
concurrency-safe (skip_locked claims + retry), duplicates degrade to no-ops.

### 1.4 `CountField` / `SumField` lost-update race in `pre_save`

**Problem.** `AggregateField.pre_save` (`denorm/fields.py:181-201`) SELECTs
the current counter value and returns it, so `save()` writes it back in the
UPDATE. The column is trigger-maintained; a trigger increment that commits
between the SELECT and the UPDATE is silently overwritten (lost update).
The SELECT is also an N+1 on every parent save.

**Change.** Never write an application-side value to a trigger-maintained
column. On INSERT return 0; on UPDATE return a no-op SQL expression so the
column keeps its current in-database value atomically:

```python
def pre_save(self, model_instance, add):
    if add:
        setattr(model_instance, self.attname, 0)
        return 0
    # Write `col = col`: resolved inside the UPDATE itself, so a concurrent
    # trigger increment between any read and this write cannot be lost.
    return models.F(self.attname)
```

Django embeds expressions returned from `pre_save` into the UPDATE
statement. Drop the extra SELECT entirely; the in-memory attribute keeps
whatever it had (same staleness contract as today — callers needing the
fresh value must `refresh_from_db()`, document this).

**Files.** `denorm/fields.py`, docs note in `docs/reference.rst`.

**Tests.**
* Staged race test in `tests/test_deadlocks.py` style: read parent, insert a
  related child (trigger increments), save parent, assert counter not reset.
* Regression: plain save of a parent leaves counter intact; new instance
  starts at 0.

**Risk.** `instance.count_field` is no longer refreshed on save. Today's
value was already capable of being stale a moment later; the contract change
must be called out in HISTORY.rst.

### 1.5 Unique-index dedup swallows invalidations while markers are claimed

**Problem.** Since migration 0017, a marker INSERT whose
`(content_type_id, COALESCE(object_id,-1), COALESCE(func_name,''))` key
collides with an **existing** row is silently dropped (the
`EXCEPTION WHEN unique_violation` handler, future `ON CONFLICT DO NOTHING`).
`flush_single` keeps its claimed markers alive in the table until the very
end of its transaction. The combination loses invalidations:

* **Concurrent writer.** While `flush_single` holds claimed marker
  `(ct, oid, X)`, a concurrent transaction updates a dependency and its
  trigger tries to insert the same key. The claimed row is committed and
  visible, so the insert hits `unique_violation` *immediately* (row locks do
  not block uniqueness checks) and is swallowed. If the flush's recompute
  statements ran *before* the writer committed, the flush then deletes the
  claimed marker and commits — the writer's invalidation is gone and the
  denormalized value stays stale until some unrelated write re-dirties the
  object.
* **Self-trigger convergence hole.** When `flush_single` processes a
  `func_name=NULL` marker (full save) and that save legitimately needs a
  follow-up pass (e.g. a same-model denorm chain with unfavorable field
  declaration order), the self-trigger's new `(ct, oid, NULL)` insert
  collides with the still-present claimed NULL marker and is swallowed —
  the follow-up pass never happens, the chained field stays stale
  permanently.

Before 0017 neither loss existed (duplicates just accumulated); 0017 traded
runaway marker growth for this race.

**Change.** Delete the claimed marker rows **immediately after claiming
them**, inside the same transaction — instead of at the end:

```python
with transaction.atomic():
    claimed = list(... .select_for_update(skip_locked=True) ...)
    if not claimed:
        return
    func_names = set(... pk__in=claimed ...)        # snapshot scope first
    DirtyInstance.objects.filter(pk__in=claimed).delete()   # free the key NOW
    obj = lock_object_or_bail()
    obj.save(**kw)
    # no trailing delete
```

Effects:

* A concurrent writer's marker insert now finds the key *deleted by an
  uncommitted transaction* → speculative wait until our commit → insert
  **succeeds** → invalidation survives and is processed next round.
* The self-trigger's follow-up NULL marker inserts successfully (its key is
  free), restoring the pre-0017 convergence semantics.
* The bail-out paths need reordering: the "object row locked elsewhere"
  check must happen *before* the delete (claim nothing, leave markers), and
  the "object deleted" path simply keeps the delete.

**Trade-off.** Writers whose trigger inserts collide with an in-flight flush
of the same `(ct, oid, func)` now block until that flush commits (short,
single-object transaction) instead of proceeding instantly. Deadlock risk
from the new wait edge is covered by the existing
`retry_on_serialization_failure` on `flush_single` and by Postgres' deadlock
detector on the writer side.

**Files.** `denorm/denorms.py` (`flush_single`).

**Tests.** Staged-race reproductions in `tests/test_deadlocks.py` style:
1. Writer-commits-mid-flush scenario above — assert the marker survives and
   a second flush round recomputes (currently: marker lost, value stale).
2. Same-model chain with reversed declaration order — assert eventual
   convergence (currently: permanently stale).

**Risk.** Behavioral change in locking (writers can briefly wait). Must ship
together with, or before, 2.5 — 2.5's convergence loop only works once the
key is freed early.

---

## Phase 2 — performance

### 2.1 Replace plpgsql `EXCEPTION` block with `ON CONFLICT DO NOTHING`

**Problem.** `TriggerActionInsert.sql` (`denorm/db/triggers.py:36-42`) wraps
every dirty-marker INSERT in `BEGIN ... EXCEPTION WHEN unique_violation`.
A plpgsql `EXCEPTION` block opens a subtransaction on **every** execution,
even when no exception fires. Subtransactions burn XIDs and, under
concurrency, hit the `pg_subtrans` SLRU bottleneck — a well-documented
Postgres scalability cliff. This fires per row, per write, on every watched
table.

**Change.** Emit instead:

```sql
INSERT INTO <table> (<columns>) <values> ON CONFLICT DO NOTHING;
```

Bare `ON CONFLICT DO NOTHING` (no conflict target) catches any unique
violation, so it does not need to name the expression index from migration
0017 and remains correct for any future unique constraints. No
subtransaction is created; speculative insertion handles the conflict before
the index entry is completed.

Works for both the `VALUES` and the `INSERT ... SELECT`
(`TriggerNestedSelect`) forms.

**Deployment note.** Triggers are reinstalled by `denorm_init` /
`denorm_rebuild_triggers`. Dedup correctness already depends on migration
0017 being applied (same as today); no new ordering requirement.

**Files.** `denorm/db/triggers.py`; regenerate expected SQL in any tests
that snapshot trigger SQL.

**Tests.** Existing dedup tests must still pass (duplicate marker insert is
a silent no-op). Add an assertion that generated SQL contains
`ON CONFLICT DO NOTHING` and no `EXCEPTION`.

**Risk.** Low. Behavior is identical for unique violations. Any *other*
error class previously... was already propagated (the handler only caught
`unique_violation`), so error semantics are unchanged.

### 2.2 Move UPDATE-trigger change-detection into `CREATE TRIGGER ... WHEN`

**Problem.** `Trigger.sql` (`denorm/db/triggers.py:95-133`) compiles the
`OLD.col IS DISTINCT FROM NEW.col OR ...` change check into an `IF` inside
the trigger *function*. Postgres must therefore invoke the plpgsql function
for every updated row, only to do nothing for rows where no watched column
changed.

**Change.** Emit the same condition as the trigger's `WHEN (...)` clause:

```sql
CREATE TRIGGER <name>
    AFTER UPDATE ON <table>
    FOR EACH ROW
    WHEN ((OLD.col1 IS DISTINCT FROM NEW.col1) OR ...)
    EXECUTE PROCEDURE f_<name>();
```

The `WHEN` clause is evaluated by the executor before the function call, so
non-matching rows skip plpgsql entirely. Keep the generic-relation
content-type conditions (`ct_field`) in `WHEN` too for UPDATE/INSERT/DELETE.

**Constraint.** `WHEN` does not allow subqueries — our conditions are plain
column comparisons, so this is fine. Statement-level triggers cannot
reference OLD/NEW in WHEN — ours are row-level.

**Caution (pre-existing behavior, do not change here).** `TriggerSet.append`
merges same-named triggers by appending *actions* only; the merged trigger
keeps the first trigger's watched-fields condition. Moving the condition to
`WHEN` preserves these semantics exactly. A follow-up issue should examine
whether per-action conditions are needed.

**Files.** `denorm/db/triggers.py`.

**Tests.** Functional: update a non-watched column, assert no DirtyInstance
appears; update a watched column, assert it does (these tests should already
exist — verify coverage). SQL snapshot test for the `WHEN` clause.

**Risk.** Low; semantics identical, evaluation point moves earlier.

### 2.3 Use the ContentType cache in `flush_single`

**Problem.** `denorms.flush_single` (`denorm/denorms.py:898`) calls
`ContentType.objects.get(pk=...)` — uncached, one query per call. A 100k-row
flush issues 100k identical queries.

**Change.** `ContentType.objects.get_for_id(content_type_id)` (process-level
cache). Additionally, `flush()` may pass the resolved `content_type=` down
to `flush_single` since it iterates grouped pairs anyway.

**Files.** `denorm/denorms.py`.

**Tests.** `assertNumQueries`-style check that two flushes of the same
content type resolve ContentType at most once.

**Risk.** None. Cache invalidation is not a concern (content types are
immutable in practice; Django core relies on the same cache).

### 2.4 Stream instead of materializing; chunk celery dispatch

**Problem.**
* `flush()` (`denorms.py:1011-1015`) loads all distinct
  `(content_type_id, object_id)` pairs into memory each pass.
* `flush_via_queue` (`tasks.py:22`) does the same *and* creates one celery
  task per pair — a 500k backlog means 500k broker messages from a single
  task.

**Change.**
* `flush()`: iterate with `.iterator(chunk_size=...)` (server-side cursor).
* `flush_via_queue`: dispatch in chunks. Change `flush_single` to accept a
  list of pairs (keep a single-pair signature for backward compat or bump
  major):

```python
@shared_task(base=Singleton, ignore_result=False, lock_expiry=...)
def flush_batch(pairs):  # [(ct_id, obj_id), ...]
    for ct_id, obj_id in pairs:
        denorms.flush_single(ct_id, obj_id)

# flush_via_queue: group(flush_batch.s(chunk) for chunk in chunks(pairs, N))
```

Chunk size via `DENORM_QUEUE_CHUNK_SIZE` (default e.g. 50). Singleton
dedup granularity becomes the chunk; per-object dedup is no longer needed
because `flush_single` is already concurrency-safe — overlapping work
degrades to skip-locked no-ops.

**Files.** `denorm/denorms.py`, `denorm/tasks.py`, `denorm/conf/settings.py`,
`denorm/management/commands/denorm_flush_via_queue.py`.

**Tests.** Update existing queue tests; regression test from S36
(`test_flush_single_task_processes_markers_inserted_after_enqueue`) must
keep passing — the chunk task still keys on logical pairs, not marker pks.

**Risk.** Medium-touch: changes task signatures. Keep the old
`flush_single` task as a thin wrapper for one release to drain in-flight
queues during deploy.

### 2.5 Collapse the self-trigger extra pass: converge inside `flush_single`

**Problem.** When a flush save actually changes a stored value, the
self-trigger (`CallbackDenorm`) inserts a fresh `(ct, oid, func_name=NULL)`
marker. It is not in `locked_pks`, so it survives the final DELETE and the
object is re-flushed on the next outer pass with a full save. Every
value-changing flush therefore costs two outer passes (two transactions, two
object locks, an extra distinct-scan).

The markers inserted by `obj.save()`'s triggers are created in *our own
transaction* and are visible to us before commit — we can process them
immediately.

**Prerequisite: item 1.5** (claimed markers deleted at claim time). Without
it, the self-trigger's follow-up NULL marker is swallowed by the unique
index whenever an identical claimed marker still exists, and there is
nothing for this loop to pick up.

**Change.** Loop inside the existing `transaction.atomic()` block until the
save produces no new markers for `(ct, oid)`:

```python
with transaction.atomic():
    obj = lock_object_or_bail()                      # before any delete
    scope = claim_and_delete_markers(ct, oid)        # item 1.5
    if not scope:
        return

    for _ in range(MAX_CONVERGE_PASSES):             # e.g. 5
        obj.save(**build_save_kwargs(scope))
        scope = claim_and_delete_markers(ct, oid)    # markers inserted by
        if not scope:                                # our own save are
            break                                    # visible in-tx; they
                                                     # are func_name=NULL →
                                                     # next pass = full save
```

Termination: each subsequent save recomputes from values just written; once
stable, the self-trigger's `IS DISTINCT FROM` condition is false, no marker
is inserted, the loop exits. The pass cap guards against non-deterministic
denorm functions; on hitting it, markers from the final save remain in the
table and the next outer pass picks them up — degraded behavior equals
current behavior.

Concurrency safety:
* Markers from concurrent **uncommitted** transactions are invisible — left
  for later, exactly as today.
* Markers from concurrent **committed** transactions may be visible to the
  re-claim statement (READ COMMITTED takes a fresh snapshot per statement).
  Claiming them is correct: the full recompute that follows reads the same
  committed state that produced them.
* A concurrent marker insert that collides with a key we claimed waits on
  our transaction (see 1.5) and lands after our commit — processed next
  round, never lost.
* The claim still uses `select_for_update(skip_locked=True)`, so we never
  block on another flush worker.

**Files.** `denorm/denorms.py` (`flush_single`).

**Tests.**
* Flush of a value-changing object empties DirtyInstance in **one**
  `flush_single` call (today: requires two).
* Staged concurrent-marker test: marker committed by "another actor" after
  the first save is either claimed-and-honored (full recompute) or left
  intact — never deleted without a recompute.
* Non-deterministic denorm function hits the cap and leaves a marker (no
  infinite loop).

**Risk.** Holds the object row lock marginally longer (one extra UPDATE).
Worth it: it halves transactions on the hot path.

### 2.6 Fix the `(content_type_id, object_id)` lookup path; drop redundant indexes

**Problem (lookup).** The unique index from 0017 is on
`(content_type_id, COALESCE(object_id, -1), COALESCE(func_name, ''))` —
the second and third keys are *expressions*. `flush_single`'s claim query
filters on the **raw** column (`WHERE content_type_id = %s AND
object_id = %s`), and Postgres only matches expression-index columns when
the query uses the syntactically same expression. Result: only the
`content_type_id` prefix is usable (equivalently via Django's automatic FK
index), and `object_id` is applied as a heap filter. With a backlog of N
dirty rows dominated by one content type — the typical rebuild/runaway
scenario — every `flush_single` claim scans O(N) entries, making a full
flush **O(N²)**.

**Change (lookup).** Make the claim/lookup queries match the expression
index instead of the raw column, via one helper used by `flush_single`:

```python
def _markers_for(content_type_id, object_id):
    return DirtyInstance.objects.filter(content_type_id=content_type_id).filter(
        _oid_c=object_id if object_id is not None else -1
    ).annotate(_oid_c=Coalesce("object_id", Value(-1)))
    # emits: WHERE content_type_id = %s AND COALESCE(object_id, -1) = %s
    # → both leading index columns become Index Conds.
```

(Exact ORM spelling to be settled in implementation — `annotate` +
`filter` on the annotation, or `.alias()`; the requirement is that the
generated SQL contains `COALESCE("denorm_dirtyinstance"."object_id", -1)`
verbatim.) Alternative if the ORM fights back: add a plain composite index
on `(content_type_id, object_id)`. On PostgreSQL ≥ 15 a cleaner long-term
option exists — a plain `UNIQUE ... NULLS NOT DISTINCT` index on the raw
columns — but the package floor is PG 12 (Django 4.2), so this is noted,
not required.

**Problem (redundant indexes).** The table is the hottest insert/delete
surface in the system; every index is write amplification and bloat:

* `func_name` (`db_index=True`) — no code path filters on it.
* `created_on` (`db_index=True`) — no code path filters on it.
* Django's automatic FK index on `content_type_id` — fully covered by the
  unique index's leading column (including FK-cascade scans on ContentType
  deletion).

**Change (indexes).** Remove `db_index=True` from `func_name` and
`created_on`; set `db_index=False` on the `content_type` FK. One
autogenerated migration `0018`. Release notes advise users who query the
table themselves to check `pg_stat_user_indexes` and re-add what they need
in their own app.

**Files.** `denorm/models.py`, `denorm/denorms.py`, new migration `0018`.

**Tests.**
* Migration applies cleanly forward/backward.
* SQL assertion that the claim query contains the `COALESCE` expression
  (guards against silent ORM regression back to a heap filter).
* Optional: `EXPLAIN`-based smoke test asserting an Index Cond on both
  leading columns.

**Risk.** Low-medium. The Coalesce-filter must be used by *every* marker
lookup in `flush_single` (claim and any re-claim in 2.5) or the O(N²)
behavior silently returns. Dropping `created_on`'s index is visible to
users with custom dashboards — called out in HISTORY.rst.

---

## Phase 3 — code health and robustness

### 3.1 `flush()` safety valve

Cap outer passes (`DENORM_MAX_FLUSH_PASSES`, default 100). On hitting the
cap, log an error naming the still-dirty content types and return instead of
looping forever on a non-deterministic denorm function. Turns a hung worker
into a diagnosable log line. (`denorm/denorms.py`, `conf/settings.py`.)

### 3.2 `denorm_flush_via_queue` command fixes

* Replace `time.sleep(0.5)` + `result.get()` with `result.get(timeout=...)`;
  document that a celery result backend is required.
* Progress bar totals: compare `completed_count()` against the number of
  dispatched tasks (chunks/pairs), not the raw DirtyInstance row count.

(`denorm/management/commands/denorm_flush_via_queue.py`.)

### 3.3 Dead code removal

* Django < 4.2 compat branches: `dependencies.py:70-73`
  (`add_lazy_relation`), `denorms.py` Django 1.8/1.9/1.10 try/excepts
  (lines ~48-50, 66-69, 494-497, 520-523), `middleware.py`
  `django.VERSION >= (1, 10)` wrapper.
* `models.py`: `DEFAULT_TIMEOUT`, `WEEK_AGO`, `find_similar`,
  `delete_similar`, `delete_this_and_similar` (unused; `delete_similar`'s
  `.select_for_update().delete()` is misleading — Django emits the DELETE
  without `FOR UPDATE`). Keep `content_object_for_update` only if the
  deadlock test that references it is kept as documentation.
* `db/triggers.py:35`: unused `denorm_queue_name` variable (NOTIFY moved to
  migration 0016).
* `contextmanagers.suppress_autotime`: superseded by the update_fields
  approach; keep only as the documented data-race exhibit for
  `tests/test_deadlocks.py` or delete both together.
* `denorms.Denorm.update()` appears unreferenced — verify and remove.

One commit per removal category; run full suite after each.

---

## Explicitly out of scope (documented future work)

* **Suppressing the self-marker for ORM saves.** Every ordinary
  `Model.save()` of a denormed model inserts a `(ct, oid, NULL)` marker even
  though `pre_save` already computed fresh values, causing one redundant
  flush re-save per write. Fixing this requires a connection-scoped flag
  (e.g. a session GUC set around the ORM save that the trigger checks),
  which interacts badly with autocommit mode (`SET LOCAL` is a no-op outside
  a transaction) and connection pooling. Revisit separately; the existing
  escape hatch is `DENORM_BULK_UNSAFE_TRIGGERS` (drops bulk-update safety).
* **Per-action WHEN conditions for merged triggers** (see 2.2 caution).
  Today, when a model has several `@denormalized` fields, their self-triggers
  share one name and `TriggerSet.append` keeps only the **first** field's
  `skip`/`only` watch-list — the other fields' conditions are silently
  ignored.
* ~~**Declarative same-model dependencies.**~~ **Promoted to a designed
  feature** — see `docs/superpowers/specs/2026-06-12-depend-on-fields-design.md`.
  Decided design goes further than the sketch that used to live here:
  self-triggers become per-function for *all* models (declared → targeted
  watch-list; undeclared → conservative watch-all), library triggers stop
  emitting `func_name=NULL` entirely, and NULL becomes an explicit-only
  whole-object marker (`rebuild_instances_of`, new `denorm.mark_dirty()`)
  that takes precedence in `flush_single`. Includes an AST-based Django
  system check auditing declarations. **Item 1.5 is a hard prerequisite**
  (the marker-swallow bug breaks this feature's cascades). Note for 2.5:
  after this feature, fresh same-object markers claimed by the convergence
  loop carry specific func names, so loop iterations stay targeted instead
  of escalating to full saves.
* **Parallelizing `flush()`** — the celery path already provides
  parallelism; keep the inline path simple.

## Rollout

1. Phase 1 → release 1.11.3 (pure bugfixes, no schema/SQL changes).
   Item 1.5 leads — it is also the hard prerequisite of the
   `@depend_on_fields` feature.
2. `@depend_on_fields` feature (separate design doc,
   `docs/superpowers/specs/2026-06-12-depend-on-fields-design.md`) →
   release 1.12.0, implemented first per project priority.
3. Phase 2 → 1.12.0 or follow-up (trigger SQL changes require
   `denorm_rebuild_triggers` after upgrade — release note), includes 0018.
4. Phase 3 riding along or as a follow-up.

Every phase: `python runtests.py postgres` + `pytest tests/` green, plus the
new tests listed per item. HISTORY.rst entry per release.
