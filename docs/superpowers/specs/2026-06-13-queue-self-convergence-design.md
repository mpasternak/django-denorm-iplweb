# Design: queue self-convergence + flush-internal NOTIFY suppression (audit #2)

Status: draft — SCOPE CONFIRMATION REQUESTED (this is the heaviest/riskiest
of the audit items; value is bounded efficiency + architectural cleanup).

## Problem (recap)

Two coupled issues:

1. **Architectural divergence.** Inline `flush()` self-converges (its outer
   `while` loop runs until the dirty table is empty). The **queue path** does
   NOT: `flush_via_queue` snapshots distinct `(ct, oid)` pairs ONCE, dispatches
   one `flush_batch` group, and returns. Cross-object cascade markers created
   *during* processing are not in that snapshot — they are rediscovered ONLY
   via the statement-level NOTIFY their INSERT fires (migration 0016) waking
   `denorm_queue` → a fresh `flush_via_queue`.

2. **Wasted NOTIFY wakeups.** The NOTIFY trigger fires on *every* marker
   INSERT, including markers inserted by the flush's own recompute saves.
   After the convergence loop (2.5) consumes same-object chain markers
   in-transaction, their INSERT still fired NOTIFY at commit → wakes the queue
   for work already done → a no-op `flush_via_queue` (Singleton-deduped, so
   bounded, but churn).

The two are coupled: we cannot suppress flush-internal NOTIFY (fix #2) without
first making the queue path self-converge (fix #1) — otherwise cross-object
cascades in the queue path would be orphaned.

## Risk / value assessment (read before building)

- **Value:** removes wasted NOTIFY→no-op-flush churn under heavy cascades, and
  removes the inline-vs-queue convergence divergence. Singleton already bounds
  the wasted wakeups, so the efficiency gain is real but not dramatic.
- **Cost/risk:** introduces a **Celery chord** (queue self-convergence loop
  with a pass cap), a **new migration** updating the NOTIFY trigger function,
  and a **session GUC** (`denorm.flushing`) with autocommit/transaction
  subtleties. Larger new failure surface than #1/#3/#4.

This is why scope confirmation is requested. Options: **(full)** A+B below;
**(A-only)** queue self-convergence, keep NOTIFY as-is (fixes the divergence,
leaves the churn — lower risk, no migration); **(defer)** document and skip.

## Part A — queue self-convergence (chord)

`flush_via_queue` dispatches the `flush_batch` group as a **chord** whose
callback re-checks the dirty table and re-dispatches `flush_via_queue` until a
round leaves nothing — making the queue path self-sufficient like inline
`flush()`, bounded by a pass cap.

```python
from celery import chord

@shared_task(base=Singleton, ignore_result=False, lock_expiry=...)
def flush_via_queue(_pass=0):
    from denorm.models import DirtyInstance
    chunks = _chunk_distinct_pairs()           # existing chunking, extracted
    if not chunks:
        return
    if _pass >= settings.DENORM_MAX_QUEUE_PASSES:
        logger.error("denorm flush_via_queue: aborting after %d passes; "
                     "still-dirty content_type_ids=%s", _pass, _remaining_cts())
        return
    return chord(
        (flush_batch.s(pairs=c) for c in chunks)
    )(_flush_requeue.s(next_pass=_pass + 1))

@shared_task(ignore_result=False)
def _flush_requeue(batch_results, next_pass):
    from denorm.models import DirtyInstance
    if DirtyInstance.objects.exists():
        flush_via_queue.delay(_pass=next_pass)
```

Notes:
- Singleton on `flush_via_queue`: the task returns the chord quickly →
  releases its lock; by the time `_flush_requeue` re-dispatches, the lock is
  free. `_pass` varies the args so re-dispatch isn't deduped against a stale
  lock.
- New setting `DENORM_MAX_QUEUE_PASSES` (default e.g. 100) — the queue analogue
  of `DENORM_MAX_FLUSH_PASSES`, bounding non-convergent denorms.
- Chord needs a result backend (Redis — already configured).
- Legacy `flush_single`/`flush_batch` tasks unchanged.

## Part B — suppress flush-internal NOTIFY (GUC + migration)

In `flush_single`'s transaction, set a session-local GUC; the NOTIFY trigger
skips when it is set. This suppresses NOTIFY for markers inserted by **any**
flush (inline or queue), since `flush_single` is the common unit — pure win
for inline (already self-converging) and safe for the queue once Part A lands.

`denorm/denorms.py` `flush_single`, inside `transaction.atomic()`, before the
save loop:

```python
from django.db import connection
connection.cursor().execute("SET LOCAL denorm.flushing = 'on'")
```

`SET LOCAL` is transaction-scoped (auto-reset at commit/rollback) and applies
to the same connection the ORM uses, so the triggers fired by `obj.save()`
within this transaction see it. No manual teardown needed.

New migration (0019) updating the NOTIFY function:

```sql
CREATE OR REPLACE FUNCTION notify_django_denorm_queue()
  RETURNS trigger AS $$
  BEGIN
    IF current_setting('denorm.flushing', true) IS DISTINCT FROM 'on' THEN
      PERFORM pg_notify('django_denorm_process', '');
    END IF;
    RETURN NEW;
  END;
  $$ LANGUAGE plpgsql;
```

`current_setting('denorm.flushing', true)` — the `true` (missing_ok) returns
NULL when unset → `IS DISTINCT FROM 'on'` → NOTIFY fires normally for genuine
writes. Only the flushing connection's marker INSERTs are suppressed
(SET LOCAL is connection+transaction scoped; concurrent user writes on other
connections are unaffected).

Reverse migration restores the unconditional NOTIFY.

## Why A is a prerequisite for B
With B alone, cross-object cascade markers created during a queue flush would
no longer NOTIFY → `denorm_queue` wouldn't wake → orphaned until the next
genuine write. A makes the queue self-converge so it no longer depends on
flush-internal NOTIFY. (Inline flush never depended on it, so B alone would be
safe for inline — but the trigger can't tell inline from queue flushes.)

## Tests (use the celery test harness on develop: live_worker / celery_redis)

1. **Queue self-converges across cascades (real worker, eager off).** Set up a
   cross-object cascade (e.g. `Post.response_count` recursive parent/child).
   Dirty a child; run the queue path end-to-end (live_worker); assert the
   parent (a different object, not in the initial snapshot) is settled — i.e.
   the chord re-dispatched and drained it WITHOUT relying on the (now
   suppressed) NOTIFY.
2. **Pass cap.** A non-convergent denorm → the chord loop stops at
   `DENORM_MAX_QUEUE_PASSES` (no infinite chord), logs, leaves markers.
3. **NOTIFY suppressed during flush.** With a real LISTEN connection (like the
   deadlock-suite reconnect test), assert: a genuine user write fires NOTIFY;
   a flush_single (its marker inserts) fires NO NOTIFY. (Capture via a second
   connection LISTENing and polling `pg_con.notifies`.)
4. **Inline flush unaffected / still converges** (regression).
5. Full suites green; new migration applies forward/backward cleanly.

## Settings / docs / migration
- `DENORM_MAX_QUEUE_PASSES` (default 100) in `denorm/conf/settings.py`.
- migration `0019` (NOTIFY function update) — requires `denorm_rebuild_triggers`
  is NOT needed (it's a denorm-app migration, applied by migrate; the function
  is replaced directly).
- docs/reference.rst + HISTORY.rst + tutorial (queue subsection).

## Out of scope
- Replacing LISTEN/NOTIFY (it's the right transactional bridge).
- Changing the inline flush convergence (already correct).
