# Celery broker test harness (feature a) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Stop stubbing the Celery queue in tests. Wire a real celery app + a Redis testcontainer so `flush_via_queue` / `flush_batch` / the `denorm_flush_via_queue` command run for real (eager mode), and Singleton dedup is tested deterministically against real Redis.

**Architecture:** A celery app in the test project, configured from Django settings (CELERY namespace). A Redis testcontainer (pytest session fixture + `run_tests_tc.py`) provides the broker/result/singleton backend. **Mixed execution by what each test needs:**
- **real in-process worker, eager OFF** for the end-to-end path (genuine broker serialization + round-trip + worker picks up `flush_via_queue` → dispatches the `flush_batch` group → `flush_single` drains the DB) — these "check more" (per the maintainer's call);
- **eager** for fast tests where async timing adds nothing (e.g. the command's group-result progress loop — spike Q3 confirms the `EagerResult` API works);
- **direct lock-backend assertions** for Singleton dedup (deterministic, no race) — eager can't show concurrent dedup (spike Q1) and a worker race would be flaky, so we assert the lock mechanism directly (spike Q4).

**Tech stack:** celery 5.6, celery-singleton 0.3.1, redis-py, testcontainers `RedisContainer`, pytest, Django test runner.

**Branch:** `celery-broker-tests` (worktree `/Users/mpasternak/Programowanie/django-denorm-iplweb-celery`).

## Spike findings (de-risking — verified, do not re-investigate)

- celery-singleton resolves its lock backend from `singleton_backend_url` → `result_backend` (if `redis://`) → `broker_url` (`celery_singleton/config.py`).
- `Singleton.apply_async` **always** acquires a Redis lock before running — so without a Redis backend it explodes (this is the T1 brittleness). With real Redis it works, **including in eager mode** (Q2).
- Eager mode does **not** dedup concurrent submissions: two sequential `apply_async` both run (the first releases the lock before the second starts) — Q1. So a real worker would be needed to see concurrent dedup; we deliberately avoid that and test the lock mechanism directly instead.
- `group(...).apply_async()` and `EagerResult` `.get()/.ready()/.completed_count()/len()` all work in eager — Q3. So the command's progress loop is testable in eager.
- `task.singleton_backend.lock(key, id, expiry)` / `.unlock(key)` against real Redis behave correctly (lock held → second lock False → after unlock True) — Q4. This is how we test dedup deterministically: identical chunks → identical `generate_lock` key.
- **Spike 2 (non-eager worker):** `celery.contrib.testing.worker.start_worker(app, pool="solo", perform_ping_check=False, loglevel="error")` starts an in-process worker against the testcontainer Redis with eager OFF, processes a `.delay()` submission via a real round-trip, and shuts down cleanly. `perform_ping_check=False` avoids needing `celery.ping` registered. This is the basis for the `live_worker` fixture.

## Environment

- pytest: `uv run pytest tests/ -q`. Django suite: `uv run python run_tests_tc.py`.
- Both currently green on develop tip (pytest 57, Django 42).
- `RedisContainer("redis:7-alpine")` — reuse the alpine tag (cached locally) to avoid a slow `redis:latest` pull.

## File structure

- Create `test_denorm_project/test_denorm_project/celery.py` — the celery app.
- Modify `test_denorm_project/test_denorm_project/__init__.py` — expose the app so `@shared_task` binds to it.
- Modify `test_denorm_project/test_denorm_project/settings.py` and `settings_postgres.py` — celery config (fix the dead `CELERY_ALWAYS_EAGER`).
- Modify `tests/conftest.py` — Redis session fixture + inject URL into `app.conf`.
- Modify `run_tests_tc.py` — start Redis alongside Postgres, export env.
- Modify `tests/test_deadlocks.py` — real fan-out test + lock-backend dedup test + un-patch the T1 reconnect test.
- Modify `test_denorm_project/test_app/tests.py` — real `denorm_queue` command test.
- Modify `HISTORY.rst`.

---

### Task 1: Celery app + settings wiring

**Files:** create `test_denorm_project/test_denorm_project/celery.py`; modify `__init__.py`, `settings.py`, `settings_postgres.py`.

- [ ] **Step 1: Create the celery app**

`test_denorm_project/test_denorm_project/celery.py`:

```python
import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "test_denorm_project.settings")

app = Celery("test_denorm_project")
# Pull CELERY_* keys from Django settings.
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
```

- [ ] **Step 2: Expose the app for @shared_task binding**

`test_denorm_project/test_denorm_project/__init__.py`:

```python
from .celery import app as celery_app

__all__ = ("celery_app",)
```

(Importing the app at package import makes it the default app, so denorm's
`@shared_task` functions bind to it.)

- [ ] **Step 3: Replace the dead CELERY_ALWAYS_EAGER in settings.py**

In `test_denorm_project/test_denorm_project/settings.py`, replace
`CELERY_ALWAYS_EAGER = True` (pre-4.0 name, inert on celery 5.x) with the
namespaced 5.x keys, reading the broker from env (set by the test fixtures):

```python
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_BROKER_URL = os.getenv("DENORM_TEST_REDIS_URL", "memory://")
CELERY_RESULT_BACKEND = os.getenv("DENORM_TEST_REDIS_URL", "cache+memory://")
```

(If `settings_postgres.py` imports from `settings.py`, no change needed
there; otherwise mirror the same block. Check the import chain first and
keep it DRY.)

- [ ] **Step 4: Verify nothing breaks at import**

Run: `uv run python -c "import test_denorm_project.celery as c; print(c.app)"` from `test_denorm_project/`.
Expected: prints the Celery app, no error.

- [ ] **Step 5: Commit**

```bash
git add test_denorm_project/test_denorm_project/celery.py test_denorm_project/test_denorm_project/__init__.py test_denorm_project/test_denorm_project/settings.py test_denorm_project/test_denorm_project/settings_postgres.py
git commit -m "test: add celery app + 5.x eager config (fix dead CELERY_ALWAYS_EAGER)"
```

---

### Task 2: Redis testcontainer fixtures

**Files:** modify `tests/conftest.py`, `run_tests_tc.py`.

- [ ] **Step 1: pytest Redis fixture**

Append to `tests/conftest.py`:

```python
@pytest.fixture(scope="session", autouse=True)
def celery_redis():
    """Real Redis for the celery queue path (broker + result + singleton
    lock backend). Eager mode runs tasks inline; the broker is still needed
    because celery-singleton acquires a Redis lock on every apply_async."""
    import os

    from testcontainers.redis import RedisContainer

    with RedisContainer("redis:7-alpine") as rc:
        url = f"redis://{rc.get_container_host_ip()}:{rc.get_exposed_port(6379)}/0"
        os.environ["DENORM_TEST_REDIS_URL"] = url

        # Inject into the live app.conf in case it was created before this
        # fixture, and drop any cached singleton backend on our tasks.
        from test_denorm_project.celery import app

        app.conf.broker_url = url
        app.conf.result_backend = url
        app.conf.task_always_eager = True
        app.conf.task_eager_propagates = True

        from denorm import tasks

        for t in (tasks.flush_single, tasks.flush_batch, tasks.flush_via_queue):
            t._singleton_backend = None
            t._singleton_config = None

        yield url
```

- [ ] **Step 2: `live_worker` fixture (non-eager, real in-process worker)**

Also append to `tests/conftest.py` (basis: spike 2):

```python
@pytest.fixture
def live_worker(celery_redis):
    """A real in-process celery worker with eager OFF, for end-to-end
    queue tests (genuine broker round-trip). Function-scoped: flips eager
    off for its duration and restores it after, so other tests keep the
    fast eager path."""
    from celery.contrib.testing.worker import start_worker

    from test_denorm_project.celery import app

    prev = app.conf.task_always_eager
    app.conf.task_always_eager = False
    try:
        with start_worker(
            app, pool="solo", perform_ping_check=False, loglevel="error"
        ):
            yield
    finally:
        app.conf.task_always_eager = prev
```

Note for tests using `live_worker`: the worker runs in its own thread with
its own DB connection, so test data must be **committed** — pair it with
`transactional_db` (not `db`), exactly like the existing thread-based
deadlock tests.

- [ ] **Step 3: Run-time check the fixtures wire up**

Run: `uv run pytest tests/test_deadlocks.py -k lock_expiry -q` (an existing celery test).
Expected: passes (and the Redis container starts once for the session).

- [ ] **Step 4: Redis in the Django runner**

In `run_tests_tc.py`, start a Redis container alongside Postgres and export
`DENORM_TEST_REDIS_URL` before running the Django tests. Mirror the existing
`PostgresContainer` block:

```python
from testcontainers.redis import RedisContainer

# inside main(), wrapping the existing pg context:
with RedisContainer("redis:7-alpine") as rc:
    redis_url = f"redis://{rc.get_container_host_ip()}:{rc.get_exposed_port(6379)}/0"
    os.environ["DENORM_TEST_REDIS_URL"] = redis_url
    # ... existing PostgresContainer block and test run ...
```

- [ ] **Step 5: Verify Django runner still green**

Run: `uv run python run_tests_tc.py` — Expected: 42 OK (Redis now also up).

- [ ] **Step 6: Commit**

```bash
git add tests/conftest.py run_tests_tc.py
git commit -m "test: Redis testcontainer + live_worker fixture for the celery queue path"
```

---

### Task 3: Real fan-out test + deterministic dedup test

**Files:** modify `tests/test_deadlocks.py`.

- [ ] **Step 1: Rewrite the fan-out test to a real NON-eager round-trip**

Replace `test_flush_via_queue_fans_out_one_task_per_duplicate` (the stubbed
version) with a genuine end-to-end async test using `live_worker` +
`transactional_db` + `denorm_triggers`. This exercises the full path —
broker serialization, the worker picking up `flush_via_queue`, its
`flush_batch` group dispatch, and `flush_single` draining the DB — which is
the point of the maintainer's "a few tests without eager" call. Drop the
`_StubGroup`/`_stub_signature` machinery.

```python
def test_flush_via_queue_drains_db_through_a_real_worker(
    transactional_db, denorm_triggers, live_worker
):
    """End-to-end, eager OFF: submit flush_via_queue over a real broker and
    let an in-process worker drain the queue. Verifies the genuine async
    path (serialization, round-trip, group fan-out, flush_single), not just
    the call shape."""
    from test_app.models import Forum, Post

    from denorm import tasks
    from denorm.models import DirtyInstance

    forum = Forum.objects.create(title="q*")
    Post.objects.create(forum=forum, title="p")
    # settle setup synchronously, then dirty deterministically
    from denorm import denorms

    denorms.flush()
    DirtyInstance.objects.all().delete()
    forum_ct = ContentType.objects.get_for_model(Forum)
    # duplicate markers for one pair — must collapse to one logical flush
    DirtyInstance.objects.create(content_type=forum_ct, object_id=forum.pk)
    DirtyInstance.objects.create(
        content_type=forum_ct, object_id=forum.pk, func_name="author_names"
    )

    tasks.flush_via_queue.delay()  # real dispatch, worker will pick it up

    deadline = time.time() + 30
    while DirtyInstance.objects.exists() and time.time() < deadline:
        time.sleep(0.2)
    assert not DirtyInstance.objects.exists(), (
        "the real worker did not drain DirtyInstance within 30s"
    )
```

(Use the existing module-level `time`/`ContentType` imports in
`test_deadlocks.py`.)

- [ ] **Step 2: Add a deterministic Singleton-dedup test (lock backend)**

```python
def test_flush_batch_singleton_dedups_identical_chunks(celery_redis):
    """Eager can't show concurrent dedup (the first task releases its lock
    before the second starts), so assert the dedup MECHANISM directly: two
    identical flush_batch chunks generate the same Singleton lock key, and
    while the lock is held a second acquire fails."""
    from denorm import tasks

    chunk = [(1, 1), (1, 2)]
    lock = tasks.flush_batch.generate_lock(tasks.flush_batch.name, [], {"pairs": chunk})
    same = tasks.flush_batch.generate_lock(tasks.flush_batch.name, [], {"pairs": chunk})
    other = tasks.flush_batch.generate_lock(
        tasks.flush_batch.name, [], {"pairs": [(1, 3)]}
    )
    assert lock == same, "identical chunks must dedup to the same lock key"
    assert lock != other, "different chunks must not collide"

    backend = tasks.flush_batch.singleton_backend
    try:
        assert backend.lock(lock, "tid-1", expiry=60) is True
        assert backend.lock(lock, "tid-2", expiry=60) is False  # held
    finally:
        backend.unlock(lock)
    assert backend.lock(lock, "tid-3", expiry=60) is True
    backend.unlock(lock)
```

- [ ] **Step 3: Un-patch the T1 reconnect test**

In `test_denorm_queue_survives_listen_connection_drop`, remove the
`patch("...flush_via_queue.delay", ...)` workaround added in T1 — with a real
Redis backend the startup kick no longer explodes. Verify the test still
passes (it should now exercise the real `.delay()` in eager).

- [ ] **Step 4: Verify**

Run: `uv run pytest tests/test_deadlocks.py -q` — all pass.

- [ ] **Step 5: Commit**

```bash
git add tests/test_deadlocks.py
git commit -m "test: real flush_via_queue fan-out + deterministic Singleton dedup; drop T1 stub"
```

---

### Task 4: Real denorm_queue command test

**Files:** modify `test_denorm_project/test_app/tests.py`.

- [ ] **Step 1: Make the command test exercise real dispatch**

The existing `CommandsTestCase.test_denorm_queue` patches `select.select` and
`flush_via_queue`. Keep the `select` patch (the LISTEN loop genuinely blocks
on it), but let `flush_via_queue` run for real (eager + Redis) instead of
asserting on a mock. Create a dirty marker before the run and assert it is
flushed after the loop's single iteration. Keep the existing call-count
intent (startup kick + one per wake-up) but verify via DB state, not mock.

- [ ] **Step 2: Verify**

Run: `uv run python run_tests_tc.py test_app.tests.CommandsTestCase` — passes.

- [ ] **Step 3: Commit**

```bash
git add test_denorm_project/test_app/tests.py
git commit -m "test: denorm_queue command flushes for real against Redis"
```

---

### Task 5: Docs + final sweep

**Files:** modify `HISTORY.rst`, `docs/reference.rst`.

- [ ] **Step 1: HISTORY note**

Add to the 1.12.0 entry: tests now run the Celery queue against a real Redis
(testcontainers, eager mode); contributors need Docker for the test suite
(already required for Postgres). No runtime/API change.

- [ ] **Step 2: reference.rst note (testing section)**

Document that the test suite spins a Redis container and runs celery eagerly,
and that contributors need Docker.

- [ ] **Step 3: Full sweep**

- `uv run pytest tests/ -q` — green.
- `uv run python run_tests_tc.py` — green.
- `uvx flake8 --max-line-length=120 denorm/ tests/` — no new violations.

- [ ] **Step 4: Commit**

```bash
git add HISTORY.rst docs/reference.rst
git commit -m "docs: note the Redis-backed celery test harness"
```

---

## Self-review notes

- Mixed execution: a real in-process worker (eager OFF) for the end-to-end
  drain test (checks the genuine async path), eager for the command's
  group-result loop (Q3), and direct lock-backend assertions for dedup (Q1
  rules out eager-dedup, a worker race would be flaky, so Q4 is the
  deterministic choice). Both spikes verified the primitives.
- Fixture ordering: the `celery_redis` fixture injects the URL into `app.conf` AND clears cached singleton backends, covering the case where the app/task backend was resolved before the fixture ran.
- `memory://` defaults in settings keep non-queue tests importable without Redis; the fixture overrides with the real URL for queue tests.
- Scope: this is test-harness only — no change to `denorm/` runtime code. If a task needs `denorm/` changes to be testable, STOP and report (it would mean a real bug, not a harness gap).
