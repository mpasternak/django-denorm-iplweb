# Audit-Spec Remaining Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the remaining items of `docs/spec-concurrency-performance-fixes.md`: Phase 1 bugfixes 1.1–1.4, Phase 2 performance items 2.1–2.4 + 2.6, Phase 3 cleanups 3.2–3.3. (1.5 and 3.1 shipped in PR #2; 2.5 deferred — per-function markers changed its calculus, needs its own design pass.)

**Architecture:** Each spec item is one self-contained task with TDD where behavior changes. Trigger-SQL items (2.1, 2.2) change generated DDL only — semantics pinned by the existing functional suite plus new SQL-shape tests. Celery fan-out (2.4) introduces a `flush_batch` task and keeps the old `flush_single` task as a deprecated wrapper for one release.

**Tech Stack:** Django 4.2+/5.2+, PostgreSQL 12+ (plpgsql triggers, `ON CONFLICT`, `CREATE TRIGGER ... WHEN`), celery + celery-singleton (verified: `Singleton.lock_expiry` supported), pytest/testcontainers.

**Branch:** `audit-spec-fixes` off `develop`.

**Environment (same as previous plan):**
- pytest: `uv run pytest tests/ -q` — currently 50 passed.
- Django suite: `uv run python run_tests_tc.py` — currently 41 OK. (NOT `runtests.py` — no local Postgres; testcontainers.)
- Lint: `uvx flake8 --max-line-length=120 denorm/ tests/` — 9 pre-existing violations in untouched legacy files; introduce none.
- makemigrations needs `DJANGO_SETTINGS_MODULE=test_denorm_project.settings_postgres`, run from `test_denorm_project/`.

---

### Task 1: Spec 1.1 + 1.2 — `denorm_queue` NOTIFY drain + startup backlog kick

**Files:**
- Modify: `denorm/management/commands/denorm_queue.py` (`_listen_loop`)
- Modify: `test_denorm_project/test_app/tests.py` (CommandsTestCase, ~line 773)

- [ ] **Step 1: Write the failing tests**

In `test_denorm_project/test_app/tests.py`, replace `test_denorm_queue` (lines 774-780) and add a drain test:

```python
    @patch("select.select")
    @patch("denorm.tasks.flush_via_queue")
    def test_denorm_queue(self, flush_via_queue, select):
        "denorm_queue kicks one flush for pre-existing backlog + one per NOTIFY wake-up."
        call_command("denorm_queue", run_once=True)
        select.assert_called_once()
        # One .delay() right after LISTEN (startup backlog, spec 1.2),
        # one after the select() wake-up.
        self.assertEqual(flush_via_queue.delay.call_count, 2)

    @patch("select.select")
    @patch("denorm.tasks.flush_via_queue")
    def test_denorm_queue_drains_notifications(self, flush_via_queue, select):
        "spec 1.1: poll()ed notifications must be drained, not accumulated forever."
        from django.db import connection

        connection.cursor()  # ensure the connection exists
        pg_con = connection.connection
        # Simulate notifications a previous poll() appended.
        pg_con.notifies.append(object())
        pg_con.notifies.append(object())

        call_command("denorm_queue", run_once=True)

        self.assertEqual(
            list(pg_con.notifies),
            [],
            "denorm_queue must drain pg_con.notifies after poll(); the list "
            "grows without bound in this long-running daemon otherwise.",
        )
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python run_tests_tc.py test_app.tests.CommandsTestCase`
Expected: `test_denorm_queue` fails (delay called once, not twice); `test_denorm_queue_drains_notifications` fails (notifies not drained).

- [ ] **Step 3: Implement**

In `denorm/management/commands/denorm_queue.py` `_listen_loop`, after the `LISTEN` execute + log line, add the backlog kick; in the loop after `pg_con.poll()`, drain:

```python
        crs.execute(f"LISTEN {const.DENORM_QUEUE_NAME}")

        logger.info("denorm_queue: listening on channel '%s'", const.DENORM_QUEUE_NAME)

        # Spec 1.2: PostgreSQL does not queue NOTIFYs for disconnected
        # listeners. Dirty rows accumulated while we were down (deploy,
        # failover) would otherwise sit until the next unrelated write —
        # kick one flush for the backlog. Singleton dedups if one is queued.
        flush_via_queue.delay()
```

```python
            # Will raise on a dead connection — propagate so handle()
            # can reconnect with backoff.
            pg_con.poll()
            # Spec 1.1: poll() appends every NOTIFY to pg_con.notifies and
            # never removes them — drain, or this daemon leaks memory under
            # sustained write traffic. The payload is empty; arrival is the
            # only signal.
            del pg_con.notifies[:]
            flush_via_queue.delay()
```

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run python run_tests_tc.py test_app.tests.CommandsTestCase` — both pass.
Run: `uv run pytest tests/test_deadlocks.py -k denorm_queue -q` — the reconnect test (section 15) still passes (the kick adds one extra `.delay()` per reconnect — verify that test doesn't pin an exact call count; adapt its assertion only if it counts calls).

- [ ] **Step 5: Commit**

```bash
git add denorm/management/commands/denorm_queue.py test_denorm_project/test_app/tests.py
git commit -m "fix: denorm_queue drains NOTIFY list and flushes pre-existing backlog (spec 1.1, 1.2)"
```

---

### Task 2: Spec 1.3 — celery-singleton lock expiry

**Files:**
- Modify: `denorm/tasks.py`
- Modify: `denorm/conf/settings.py`
- Test: `tests/test_deadlocks.py` (append)

- [ ] **Step 1: Write the failing test** (append to `tests/test_deadlocks.py`, new section 17)

```python
# ---------------------------------------------------------------------------
# 17. Spec 1.3: celery-singleton locks must expire.
# ---------------------------------------------------------------------------


def test_singleton_tasks_carry_lock_expiry():
    """Without lock_expiry, a SIGKILLed worker leaves the Redis lock
    forever and that (content_type, object) pair can never be enqueued
    again — denormalization for the object silently stops."""
    from denorm import tasks
    from denorm.conf import settings as denorm_settings

    assert denorm_settings.DENORM_SINGLETON_LOCK_EXPIRY == 600
    for task in (tasks.flush_single, tasks.flush_via_queue):
        assert task.lock_expiry == denorm_settings.DENORM_SINGLETON_LOCK_EXPIRY, (
            f"{task.name} has no lock_expiry; a crashed worker permanently "
            "wedges this Singleton."
        )
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_deadlocks.py -k lock_expiry -v`
Expected: FAIL (`AttributeError` on settings or `lock_expiry` is None).

- [ ] **Step 3: Implement**

`denorm/conf/settings.py`:

```python
# Redis lock TTL (seconds) for celery-singleton tasks. Without an expiry,
# a SIGKILLed worker leaves its lock forever and the affected object can
# never be enqueued again.
DENORM_SINGLETON_LOCK_EXPIRY = getattr(settings, "DENORM_SINGLETON_LOCK_EXPIRY", 600)
```

`denorm/tasks.py` — add the import and the kwarg on BOTH tasks:

```python
from denorm.conf import settings

@shared_task(
    base=Singleton,
    ignore_result=False,
    lock_expiry=settings.DENORM_SINGLETON_LOCK_EXPIRY,
)
```

(A flush legitimately outliving the expiry just allows a duplicate task — safe, `flush_single` is concurrency-safe via skip_locked claims; note this in a one-line comment.)

- [ ] **Step 4: Verify** — `uv run pytest tests/test_deadlocks.py -k lock_expiry -v` passes; full `uv run pytest tests/ -q` still 51+ passed.

- [ ] **Step 5: Commit**

```bash
git add denorm/tasks.py denorm/conf/settings.py tests/test_deadlocks.py
git commit -m "fix: expire celery-singleton locks so crashed workers cannot wedge a pair (spec 1.3)"
```

---

### Task 3: Spec 1.4 — `CountField`/`SumField` lost-update race

**Files:**
- Modify: `denorm/fields.py:181-201` (`AggregateField.pre_save`)
- Test: `tests/test_deadlocks.py` (append, section 18)

- [ ] **Step 1: Write the failing race test**

```python
# ---------------------------------------------------------------------------
# 18. Spec 1.4: AggregateField.pre_save read-then-write loses concurrent
# trigger increments.
# ---------------------------------------------------------------------------


def test_countfield_save_does_not_clobber_concurrent_increment(
    transactional_db, denorm_triggers
):
    """AggregateField.pre_save SELECTs the trigger-maintained counter and
    save() writes that value back. An increment committed between the
    SELECT and the UPDATE is silently overwritten. Fix: write
    `col = col` (an F() expression) so the UPDATE can never lose
    concurrent increments.

    Staged deterministically: a hook between pre_save and the UPDATE
    commits a child insert (trigger increments the counter), then the
    parent save proceeds.
    """
    from test_app.models import Forum, Post

    from denorm import denorms
    from denorm.fields import AggregateField
    from denorm.models import DirtyInstance

    forum = Forum.objects.create(title="cnt")
    denorms.flush()
    DirtyInstance.objects.all().delete()
    forum.refresh_from_db()
    assert forum.post_count == 0

    orig_pre_save = AggregateField.pre_save
    state = {"fired": False}

    def racing_pre_save(self, instance, add):
        value = orig_pre_save(self, instance, add)
        if not add and not state["fired"]:
            state["fired"] = True

            def writer():
                from django.db import connections

                try:
                    # Own thread = own connection (autocommit): the child
                    # commits and its trigger increments forum.post_count
                    # BEFORE the parent's UPDATE executes.
                    Post.objects.create(forum_id=instance.pk, title="mid-save")
                finally:
                    for alias in connections:
                        try:
                            connections[alias].close()
                        except Exception:
                            pass

            t = threading.Thread(target=writer, daemon=True)
            t.start()
            t.join(timeout=30)
            assert not t.is_alive(), "writer hung — unexpected lock"
        return value

    with patch.object(AggregateField, "pre_save", racing_pre_save):
        forum.save()

    count_in_db = Forum.objects.values_list("post_count", flat=True).get(
        pk=forum.pk
    )
    assert count_in_db == 1, (
        "Parent save() overwrote the trigger-maintained counter with the "
        "value read before the concurrent increment committed (lost "
        "update). pre_save must emit `col = col`, not a snapshot value."
    )
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_deadlocks.py -k clobber -v`
Expected: FAIL with `assert 0 == 1` (the increment was clobbered).

- [ ] **Step 3: Implement**

Replace `AggregateField.pre_save` in `denorm/fields.py`:

```python
    def pre_save(self, model_instance, add):
        """Never write an application-side snapshot to this
        trigger-maintained column.

        On INSERT there can be no related rows yet -> 0. On UPDATE return
        `F(column)` so the SQL reads `col = col`: resolved inside the
        UPDATE itself, a concurrent trigger increment between any read
        and this write cannot be lost. The in-memory attribute is NOT
        refreshed by save(); callers needing the current value must
        refresh_from_db().
        """
        if add:
            setattr(model_instance, self.attname, 0)
            return 0
        return models.F(self.attname)
```

Do NOT setattr the F() on the instance. Delete the old SELECT (`self.denorm.model.objects.filter(...).values_list(...)`) entirely.

- [ ] **Step 4: Verify**

Run: `uv run pytest tests/test_deadlocks.py -k clobber -v` — passes.
Run: `uv run pytest tests/ -q` and `uv run python run_tests_tc.py` — fully green. The Django suite has CountField/SumField tests (`FilterCountModel`, `FilterSumModel`, `Forum.post_count`); failures there mean Django rejected the expression-from-pre_save path — investigate (Django supports expressions returned from `pre_save`; if a specific assertion reads `instance.post_count` right after save and now sees a stale value, update that assertion to `refresh_from_db()` first and note it — that's the documented contract change).

- [ ] **Step 5: Commit**

```bash
git add denorm/fields.py tests/test_deadlocks.py test_denorm_project/
git commit -m "fix: CountField/SumField saves no longer clobber concurrent trigger increments (spec 1.4)"
```

---

### Task 4: Spec 2.1 — `ON CONFLICT DO NOTHING` instead of plpgsql EXCEPTION subtransaction

**Files:**
- Modify: `denorm/db/triggers.py` (`TriggerActionInsert.sql`, ~lines 23-43)
- Test: `tests/test_depend_on_fields.py` (`TestTriggerSetShape`, append one test)

- [ ] **Step 1: Write the failing SQL-shape test** (append to `TestTriggerSetShape`)

```python
    def test_marker_inserts_use_on_conflict_not_subtransaction(self, db):
        """A plpgsql EXCEPTION block opens a subtransaction on EVERY
        execution — a known Postgres scalability cliff (pg_subtrans SLRU)
        on hot write paths. Bare ON CONFLICT DO NOTHING has identical
        dedup semantics with no subtransaction (spec 2.1)."""
        from denorm.denorms import build_triggerset
        from denorm.models import DirtyInstance

        ts = build_triggerset()
        table = DirtyInstance._meta.db_table
        checked = 0
        for trigger in ts.triggers.values():
            for action in trigger.actions:
                sql, _ = action.sql()
                if table in sql and "INSERT" in sql.upper():
                    checked += 1
                    assert "ON CONFLICT DO NOTHING" in sql
                    assert "EXCEPTION" not in sql.upper()
        assert checked > 0
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_depend_on_fields.py -k on_conflict -v` → FAIL (EXCEPTION present).

- [ ] **Step 3: Implement**

Replace `TriggerActionInsert.sql` in `denorm/db/triggers.py`:

```python
class TriggerActionInsert(base.TriggerActionInsert):
    def sql(self):
        table = self.model._meta.db_table
        columns = "(" + ", ".join(self.columns) + ")"
        params = []
        if isinstance(self.values, TriggerNestedSelect):
            sql, nested_params = self.values.sql()
            values = "(" + sql + ")"
            params.extend(nested_params)
        else:
            values = "VALUES (" + ", ".join(self.values) + ")"

        # Bare ON CONFLICT DO NOTHING (no conflict target): catches any
        # unique violation without naming the 0017 expression index, and —
        # unlike the old EXCEPTION WHEN unique_violation block — opens no
        # subtransaction per row (pg_subtrans SLRU contention under load).
        sql = "INSERT INTO %(table)s %(columns)s %(values)s ON CONFLICT DO NOTHING" % locals()
        return sql, params
```

This also removes the unused `denorm_queue_name = const.DENORM_QUEUE_NAME` line (spec 3.3 leftover); drop the now-unused `const` import if nothing else in the file uses it (check first).

- [ ] **Step 4: Verify**

`uv run pytest tests/ -q` and `uv run python run_tests_tc.py` — fully green (dedup semantics identical; functional dedup is pinned by existing tests, e.g. mark_dirty idempotency and bulk-update marker tests, which run against real installed triggers).

- [ ] **Step 5: Commit**

```bash
git add denorm/db/triggers.py tests/test_depend_on_fields.py
git commit -m "perf: marker inserts use ON CONFLICT DO NOTHING, no per-row subtransaction (spec 2.1)"
```

---

### Task 5: Spec 2.2 — move change-detection into `CREATE TRIGGER ... WHEN`

**Files:**
- Modify: `denorm/db/triggers.py` (`Trigger.sql`, ~lines 70-164)
- Test: `tests/test_depend_on_fields.py` (`TestTriggerSetShape`, append)

- [ ] **Step 1: Write the failing SQL-shape test**

```python
    def test_update_trigger_conditions_live_in_when_clause(self, db):
        """Postgres evaluates CREATE TRIGGER ... WHEN before invoking the
        trigger function: rows that touch no watched column skip plpgsql
        entirely (spec 2.2). The IF used to live inside the function."""
        from denorm.denorms import build_triggerset

        ts = build_triggerset()
        profile_updates = [
            t
            for t in ts.triggers.values()
            if t.db_table == "test_app_profile" and t.event == "update"
        ]
        assert profile_updates
        for trigger in profile_updates:
            sql, _ = trigger.sql()
            assert "WHEN (" in sql, "UPDATE trigger lost its WHEN clause"
            assert "IS DISTINCT FROM" in sql.split("CREATE TRIGGER")[1], (
                "change-detection must sit in the CREATE TRIGGER WHEN "
                "clause, after the function definition"
            )
            body = sql.split("$$")[1]  # the plpgsql function body
            assert "IF " not in body, (
                "function body still carries the IF — condition must move "
                "to the WHEN clause so non-matching rows never invoke "
                "plpgsql"
            )
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_depend_on_fields.py -k when_clause -v` → FAIL.

- [ ] **Step 3: Implement**

In `Trigger.sql` (`denorm/db/triggers.py`): keep building `conditions` exactly as today (UPDATE field comparisons; content-type conditions for generic relations on UPDATE/INSERT/DELETE). Then, instead of wrapping `actions` in an `IF ... THEN ... END IF;` inside the function body, emit the conditions as a `WHEN` clause on `CREATE TRIGGER`:

```python
        if conditions:
            when = "WHEN (%s)\n    " % " AND ".join(conditions)
        else:
            when = ""
        actions = "\n        ".join(action_list)

        comment = ""
        spaces = "        "
        if self.func:
            comment = (
                f"-- Trigger generated by django-denorm-iplweb for {self.func.__qualname__}\n"
                f"{spaces}-- It happens {self.time.upper()} {self.event.upper()} on {self.db_table}\n"
                f"{spaces}-- This function was autogenerated by code found in {self.__class__}\n\n{spaces}"
            )

        sql = (
            """
CREATE OR REPLACE FUNCTION f_%(name)s()
    RETURNS TRIGGER AS $$
    BEGIN
        %(comment)s%(actions)s
        RETURN NULL;
    END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS %(name)s ON %(table)s;

CREATE TRIGGER %(name)s
    %(time)s %(event)s ON %(table)s
    FOR EACH ROW
    %(when)sEXECUTE PROCEDURE f_%(name)s();
"""
            % locals()
        )
        return sql, params
```

(Adapt surrounding code minimally; the `conditions` construction and everything above it stays. Note `WHEN` may reference NEW only on INSERT and OLD only on DELETE — the existing condition builder already respects that. `WHEN` cannot contain subqueries — ours are plain column comparisons.)

- [ ] **Step 4: Verify**

`uv run pytest tests/ -q` and `uv run python run_tests_tc.py` — fully green. The functional skip/only tests (SkipComment* models in the Django suite) are the real guard: they prove conditions still fire/not-fire correctly from the WHEN clause.

- [ ] **Step 5: Commit**

```bash
git add denorm/db/triggers.py tests/test_depend_on_fields.py
git commit -m "perf: UPDATE-trigger change detection moves to CREATE TRIGGER WHEN (spec 2.2)"
```

---

### Task 6: Spec 2.3 + 2.4a — ContentType cache and streaming iteration in `flush()`

**Files:**
- Modify: `denorm/denorms.py` (`flush_single` head, `flush()` inner loop)
- Test: `tests/test_depend_on_fields.py` (append a small class)

- [ ] **Step 1: Write the failing test**

```python
class TestFlushQueryEfficiency:
    def test_flush_single_uses_contenttype_cache(self, transactional_db, denorm_triggers):
        """ContentType.objects.get(pk=...) bypasses Django's ContentType
        cache — a 100k-marker flush issues 100k identical queries.
        get_for_id() hits the per-process cache (spec 2.3)."""
        from unittest.mock import patch

        from test_app.models import Profile

        from denorm import denorms
        from denorm.models import DirtyInstance

        p = Profile.objects.create(first_name="A", last_name="B")
        denorms.flush()
        DirtyInstance.objects.all().delete()
        ct = ContentType.objects.get_for_model(Profile)
        DirtyInstance.objects.create(
            content_type=ct, object_id=p.pk, func_name="full_name"
        )

        with patch.object(
            ContentType.objects, "get_for_id", wraps=ContentType.objects.get_for_id
        ) as spy:
            denorms.flush_single(ct.pk, p.pk)  # no content_type kwarg

        spy.assert_called_once_with(ct.pk)
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_depend_on_fields.py -k contenttype_cache -v` → FAIL (get_for_id never called).

- [ ] **Step 3: Implement**

In `flush_single`, replace `content_type = ContentType.objects.get(pk=content_type_id)` with `content_type = ContentType.objects.get_for_id(content_type_id)`.

In `flush()`, stream the distinct pairs instead of materializing them (spec 2.4a) — the inner loop becomes:

```python
            processed = 0
            for content_type_id, object_id in (
                DirtyInstance.objects.all()
                .values_list("content_type_id", "object_id")
                .distinct()
                .iterator(chunk_size=2000)
            ):
                flush_single(content_type_id, object_id)
                processed += 1
```

- [ ] **Step 4: Verify** — targeted test passes; full suites green.

- [ ] **Step 5: Commit**

```bash
git add denorm/denorms.py tests/test_depend_on_fields.py
git commit -m "perf: flush uses ContentType cache and server-side cursor (spec 2.3, 2.4)"
```

---

### Task 7: Spec 2.4b + 3.2 — chunked celery dispatch + `denorm_flush_via_queue` command fixes

**Files:**
- Modify: `denorm/tasks.py`
- Modify: `denorm/conf/settings.py`
- Modify: `denorm/management/commands/denorm_flush_via_queue.py`
- Modify: `tests/test_deadlocks.py` (section 9 test — update to chunked semantics)

- [ ] **Step 1: Update the fan-out test (red first)**

`tests/test_deadlocks.py` section 9 (`test_flush_via_queue_fans_out_one_task_per_duplicate`, ~line 555): it currently pins "one `flush_single` subtask per distinct pair". New contract: distinct pairs are dispatched in chunks of `DENORM_QUEUE_CHUNK_SIZE` via a `flush_batch` task. Rewrite the test's capture to stub `tasks.flush_batch.s` instead of `tasks.flush_single.s`, and assert:

```python
    # all distinct pairs are covered exactly once, in ceil(n/chunk) batches
    from denorm.conf import settings as denorm_settings

    flat = [pair for chunk in captured_chunks for pair in chunk]
    distinct = list(
        DirtyInstance.objects.values_list("content_type_id", "object_id").distinct()
    )
    assert sorted(flat) == sorted(distinct)
    assert all(len(c) <= denorm_settings.DENORM_QUEUE_CHUNK_SIZE for c in captured_chunks)
```

Keep the test's existing setup (duplicate markers for one forum) and its docstring updated: duplicates must still collapse to ONE pair total. The section 9b regression test (`test_flush_single_task_processes_markers_inserted_after_enqueue`) stays UNCHANGED — the legacy `flush_single` task keeps working as a wrapper.

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_deadlocks.py -k fans_out -v` → FAIL (no flush_batch).

- [ ] **Step 3: Implement**

`denorm/conf/settings.py`:

```python
# Number of (content_type_id, object_id) pairs handled by one celery task.
DENORM_QUEUE_CHUNK_SIZE = getattr(settings, "DENORM_QUEUE_CHUNK_SIZE", 50)
```

`denorm/tasks.py` (full new content — preserves the existing flush_single contract as a wrapper):

```python
from celery import group, shared_task
from celery_singleton import Singleton

from denorm import denorms
from denorm.conf import settings


@shared_task(
    base=Singleton,
    ignore_result=False,
    lock_expiry=settings.DENORM_SINGLETON_LOCK_EXPIRY,
)
def flush_single(content_type_id: int, object_id: int):
    # Legacy single-pair task. Kept (one release minimum) so tasks already
    # sitting in brokers during a rolling deploy still execute. New code
    # dispatches flush_batch.
    denorms.flush_single(content_type_id, object_id)
    return True


@shared_task(
    base=Singleton,
    ignore_result=False,
    lock_expiry=settings.DENORM_SINGLETON_LOCK_EXPIRY,
)
def flush_batch(pairs):
    # One task per chunk of logical (content_type_id, object_id) pairs:
    # a 500k-row backlog must not become 500k broker messages. Overlap
    # with other workers degrades to skip_locked no-ops in flush_single.
    for content_type_id, object_id in pairs:
        denorms.flush_single(content_type_id, object_id)
    return True


@shared_task(
    base=Singleton,
    ignore_result=False,
    lock_expiry=settings.DENORM_SINGLETON_LOCK_EXPIRY,
)
def flush_via_queue():
    from denorm.models import DirtyInstance

    chunk_size = settings.DENORM_QUEUE_CHUNK_SIZE
    chunks = []
    chunk = []
    for pair in (
        DirtyInstance.objects.values_list("content_type_id", "object_id")
        .distinct()
        .iterator(chunk_size=2000)
    ):
        chunk.append(pair)
        if len(chunk) >= chunk_size:
            chunks.append(chunk)
            chunk = []
    if chunk:
        chunks.append(chunk)

    if chunks:
        job = group(flush_batch.s(pairs=c) for c in chunks)
        return job.apply_async()
```

`denorm/management/commands/denorm_flush_via_queue.py` (spec 3.2) — replace the sleep+get dance:

```python
import time

from django.core.management.base import BaseCommand, CommandError
from tqdm import tqdm

from denorm.models import DirtyInstance
from denorm.tasks import flush_via_queue


class Command(BaseCommand):
    help = (
        "Recalculates the value of every denormalized field that was marked "
        "dirty, using Celery queues. Requires a configured Celery result "
        "backend (the command waits on the dispatched task group)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--timeout",
            type=float,
            default=300.0,
            help="Seconds to wait for the dispatch task (default 300).",
        )

    def handle(self, timeout=300.0, **kwargs):
        total_rows = DirtyInstance.objects.count()
        if total_rows == 0:
            self.stdout.write(self.style.SUCCESS("No dirty instances to flush."))
            return

        self.stdout.write(f"Flushing {total_rows} dirty instance rows...")

        result = flush_via_queue.apply_async()
        try:
            group_result = result.get(timeout=timeout)
        except Exception as exc:  # no result backend, timeout, broker down
            raise CommandError(
                f"Could not obtain dispatch result ({exc!r}). This command "
                "requires a Celery result backend."
            )

        if group_result is None:
            self.stdout.write(self.style.SUCCESS("No tasks to process."))
            return

        total_tasks = len(group_result)
        with tqdm(total=total_tasks, desc="Flushing", unit="batch") as pbar:
            while not group_result.ready():
                pbar.n = group_result.completed_count()
                pbar.refresh()
                time.sleep(0.1)
            pbar.n = total_tasks
            pbar.refresh()

        self.stdout.write(
            self.style.SUCCESS(f"Successfully flushed {total_rows} dirty rows.")
        )
```

- [ ] **Step 4: Verify** — `uv run pytest tests/test_deadlocks.py -k "fans_out or markers_inserted_after_enqueue" -v` both pass; full suites green.

- [ ] **Step 5: Commit**

```bash
git add denorm/tasks.py denorm/conf/settings.py denorm/management/commands/denorm_flush_via_queue.py tests/test_deadlocks.py
git commit -m "perf: chunked celery dispatch; robust denorm_flush_via_queue progress (spec 2.4, 3.2)"
```

---

### Task 8: Spec 2.6 — fix the O(N²) marker lookup; drop redundant indexes

**Files:**
- Modify: `denorm/denorms.py` (`_claim_and_delete_markers`)
- Modify: `denorm/models.py`
- Create: `denorm/migrations/0018_*.py` (generated)
- Test: `tests/test_depend_on_fields.py` (append to `TestFlushQueryEfficiency`)

- [ ] **Step 1: Write the failing SQL-shape test**

```python
    def test_marker_claim_matches_expression_index(self, db):
        """The 0017 unique index keys on COALESCE(object_id, -1); a filter
        on raw object_id can only use the content_type prefix, making a
        large single-model flush O(N^2). The claim query must emit the
        same COALESCE expression so both index columns are usable
        (spec 2.6)."""
        from denorm.denorms import _markers_for

        sql = str(_markers_for(42, 7).query)
        assert 'COALESCE("denorm_dirtyinstance"."object_id", -1)' in sql
```

- [ ] **Step 2: Run to verify failure** — ImportError (`_markers_for` doesn't exist).

- [ ] **Step 3: Implement the lookup helper**

`denorm/denorms.py`, above `_claim_and_delete_markers`:

```python
def _markers_for(content_type_id, object_id):
    """All markers for the logical pair, filtered so the 0017 expression
    index is fully usable.

    The unique index keys on COALESCE(object_id, -1); filtering the raw
    column would fall back to scanning the whole content-type prefix —
    O(backlog) per object, O(backlog^2) per flush.
    """
    from django.db.models import Value
    from django.db.models.functions import Coalesce

    from .models import DirtyInstance

    return DirtyInstance.objects.alias(
        _oid=Coalesce("object_id", Value(-1))
    ).filter(
        content_type_id=content_type_id,
        _oid=-1 if object_id is None else object_id,
    )
```

In `_claim_and_delete_markers`, replace the claim queryset's
`DirtyInstance.objects.filter(content_type_id=..., object_id=...)` with
`_markers_for(content_type_id, object_id)` (the `select_for_update`/
`values_list` chain stays). The `pk__in` follow-ups stay as they are.

- [ ] **Step 4: Drop the redundant indexes**

`denorm/models.py`:
- `content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE, db_index=False)` — the 0017 unique index's leading column covers FK-cascade scans.
- `func_name = models.TextField(blank=True, null=True)` (drop `db_index=True`).
- `created_on = models.DateTimeField(auto_now_add=True)` (drop `db_index=True`).

Add a short comment block above the model noting the 0017 expression unique index is the intended access path (claim queries must go through `_markers_for`).

Generate the migration:

```bash
cd test_denorm_project && DJANGO_SETTINGS_MODULE=test_denorm_project.settings_postgres uv run python manage.py makemigrations denorm && cd ..
```

Expected: `denorm/migrations/0018_*.py` with three `AlterField` operations, nothing else.

- [ ] **Step 5: Verify**

- `uv run pytest tests/ -q` and `uv run python run_tests_tc.py` — fully green (migrations apply in the test DB bootstrap).
- `cd test_denorm_project && DJANGO_SETTINGS_MODULE=test_denorm_project.settings_postgres uv run python manage.py makemigrations denorm test_app --check --dry-run` → "No changes detected".

- [ ] **Step 6: Commit**

```bash
git add denorm/denorms.py denorm/models.py denorm/migrations/ tests/test_depend_on_fields.py
git commit -m "perf: marker claims use the 0017 expression index; drop 3 redundant indexes (spec 2.6)"
```

---

### Task 9: Spec 3.3 — dead-code removal (one commit per category)

**Files:** `denorm/middleware.py`, `denorm/dependencies.py`, `denorm/denorms.py`, `denorm/models.py`

Run `uv run pytest tests/ -q && uv run python run_tests_tc.py` after EACH commit; all green every time.

- [ ] **Step 1: Django<4.2 compat branches — commit 1**

- `denorm/middleware.py`: collapse the version-conditional double class into one `class DenormMiddleware(deprecation.MiddlewareMixin)` definition (keep the docstring); drop `import django` if now unused.
- `denorm/dependencies.py` (~lines 60-76): remove the `except AttributeError: related.add_lazy_relation(...)` Django<2.0 fallback — keep the `lazy_related_operation` path unconditionally.
- `denorm/denorms.py`: in `many_to_many_pre_save`, replace the `try: remote = m2m.remote_field / except AttributeError: remote = m2m.rel` with direct `remote = m2m.remote_field`, and the `try: ...set(values) / except AttributeError (Django<1.10)` with the direct `.set(values)` call. In `AggregateDenorm.get_triggers` (~lines 494-497 and 520-523 pre-branch numbering), keep only the Django>=1.9 attribute paths (`self.manager.field`, `self.manager.field.model`) — delete the `except AttributeError` fallbacks. GREP FIRST: `grep -n "Django<\|Django>=1\|add_lazy_relation\|m2m\.rel\b" denorm/` to catch any the spec list missed; remove only what the suite proves dead (suite green = proof).

```bash
git add -A && git commit -m "chore: drop Django<4.2 compatibility branches (spec 3.3)"
```

- [ ] **Step 2: `DirtyInstance` dead helpers — commit 2**

`denorm/models.py`: delete `DEFAULT_TIMEOUT`, `WEEK_AGO`, `find_similar`, `delete_similar`, `delete_this_and_similar` (and the now-unused `timedelta` import). KEEP `content_object_for_update` — `tests/test_deadlocks.py` uses it as a documented locking exhibit. KEEP `denorm/contextmanagers.py` (`suppress_autotime`) — `tests/test_deadlocks.py` keeps it as the documented data-race exhibit; add a one-line module docstring note: "Retained only as a documented anti-pattern exhibit for tests/test_deadlocks.py; not used by the library."

GREP FIRST: `grep -rn "find_similar\|delete_similar\|delete_this_and_similar\|DEFAULT_TIMEOUT\|WEEK_AGO" --include="*.py" .` — the only hits must be models.py itself.

```bash
git add -A && git commit -m "chore: remove unused DirtyInstance helpers and constants (spec 3.3)"
```

- [ ] **Step 3: `Denorm.update()` — commit 3 (verify first)**

`grep -rn "\.update(" denorm/ | grep -v "objects\.\|dict\|kwargs\|self\.fields"` and inspect: if `Denorm.update` (denorm/denorms.py, the method taking `instance` and diffing old/new values) has zero callers in `denorm/`, `tests/`, and `test_denorm_project/`, delete it. If a caller exists, leave it and note the finding in the report instead.

```bash
git add -A && git commit -m "chore: remove unused Denorm.update (spec 3.3)"
```

---

### Task 10: Docs, changelog, final sweep

**Files:** `HISTORY.rst`, `docs/reference.rst`, `docs/spec-concurrency-performance-fixes.md`

- [ ] **Step 1: HISTORY.rst** — extend the existing `1.12.0 (unreleased)` entry with bullets for: 1.1/1.2 (queue drain + backlog kick), 1.3 (`DENORM_SINGLETON_LOCK_EXPIRY`), 1.4 (CountField/SumField `F()` writes — **contract note**: the in-memory attribute is no longer refreshed by `save()`, call `refresh_from_db()`), 2.1/2.2 (trigger SQL: ON CONFLICT + WHEN — re-run `denorm_rebuild_triggers`, already required for 1.12.0), 2.3/2.4 (ContentType cache, streaming, `DENORM_QUEUE_CHUNK_SIZE`, new `flush_batch` task — note brokers draining old `flush_single` tasks keep working), 2.6 (expression-index claims; dropped `func_name`/`created_on`/FK indexes — users filtering those columns themselves should re-add indexes in their own apps), 3.2/3.3.

- [ ] **Step 2: docs/reference.rst** — add `DENORM_SINGLETON_LOCK_EXPIRY` and `DENORM_QUEUE_CHUNK_SIZE` to the Settings section; add the CountField/SumField `refresh_from_db()` staleness note near their field docs.

- [ ] **Step 3: Mark the spec items shipped** — in `docs/spec-concurrency-performance-fixes.md`, prefix each completed item heading (1.1, 1.2, 1.3, 1.4, 2.1, 2.2, 2.3, 2.4, 2.6, 3.2, 3.3) with `✅ SHIPPED (1.12.0) — `, and add a one-line note under 2.5 that it remains open and its design needs revisiting post-per-function-markers.

- [ ] **Step 4: Final sweep**

- `uv run pytest tests/ -q` — fully green.
- `uv run python run_tests_tc.py` — OK.
- `uvx flake8 --max-line-length=120 denorm/ tests/` — no NEW violations (some pre-existing ones may have disappeared with deleted dead code — that's fine).
- `cd test_denorm_project && DJANGO_SETTINGS_MODULE=test_denorm_project.settings_postgres uv run python manage.py makemigrations denorm test_app --check --dry-run` → no changes.

- [ ] **Step 5: Commit**

```bash
git add HISTORY.rst docs/
git commit -m "docs: changelog and reference for audit-spec fixes; mark spec items shipped"
```

---

## Self-review notes

- Spec coverage: 1.1+1.2→T1, 1.3→T2, 1.4→T3, 2.1→T4, 2.2→T5, 2.3+2.4a→T6, 2.4b+3.2→T7, 2.6→T8, 3.3→T9, docs→T10. 1.5/3.1 shipped previously; 2.5 explicitly deferred (T10 Step 3 documents it).
- Type consistency: `_markers_for` defined T8 and used only there; `flush_batch(pairs)` signature consistent between T7 task code and test stub; `DENORM_SINGLETON_LOCK_EXPIRY` defined T2, referenced T7's tasks.py listing (T7 depends on T2 — execute in order).
- Ordering constraint: T7 must run after T2 (lock_expiry setting exists). T4 and T5 both edit `denorm/db/triggers.py` — run sequentially as numbered.
