Changelog
=========

Unreleased
----------

* ``CountField`` / ``SumField`` declared over a ``ManyToManyField`` now
  work. ``AggregateDenorm.m2m_triggers()`` — the through-table triggers, the
  only ones that see a row being added to or removed from an m2m relation —
  was reached exclusively from that configuration, and it had been dead since
  Django 1.8: ``get_related_where()`` called ``manager.related`` (gone),
  ``Query.add_count_column()`` (removed in 1.8) and
  ``clear_ordering(force_empty=True)`` (renamed in 4.0), so building the
  triggers raised ``AttributeError`` at ``denorm_init`` /
  ``install_triggers()`` time. The subquery is now built with the current
  ``Query`` API (``add_annotation(Count("*"), ...)``, ``clear_ordering(force=True)``).
  ``SumDenorm``'s related increment/decrement additionally selected
  ``self.fieldname`` — the denormalized column, which lives on the *other*
  model — instead of the summed column ``self.sum_field``.

1.13.0 (2026-08-07)
-------------------

* Added support for Django 6.1. CI now tests Django 5.2, 6.0 and 6.1;
  Django 6.1 (like 6.0) requires Python 3.12+.
* fix: Django 6.1 compatibility — filtered aggregates
  (``CountField``/``SumField`` with ``filter=``/``exclude=``) generated broken
  trigger DDL. Django 6.1 changed ``Col.as_sql()`` to use
  ``SQLCompiler.quote_name()`` instead of the now-deprecated
  ``quote_name_unless_alias()``, so the fake ``NEW`` / ``OLD`` alias used to
  compile the filter came out as ``"NEW"."col"`` — a table reference, which
  PostgreSQL rejects with ``missing FROM-clause entry for table "NEW"``. The
  new ``TriggerSQLCompiler`` keeps those two PL/pgSQL record variables
  unquoted on every supported Django version.
* fix: ``Trigger.sql()`` now quotes the table name in the generated
  ``CREATE TRIGGER ... ON <table>`` / ``DROP TRIGGER ... ON <table>`` DDL.
  It was the one identifier in the trigger generator still interpolated raw
  (every ``TriggerAction*`` body already quoted its table), so a model with a
  reserved-word or mixed-case ``db_table`` produced broken trigger DDL at
  ``denorm_init`` / ``denorm_rebuild_triggers`` time.
* fix: the M2M ``CacheKeyField`` trigger now quotes the primary-key column in
  its ``<pk> IN (SELECT ...)`` ``WHERE`` clause. A model whose PK column needs
  quoting (reserved word / mixed case) produced broken SQL.
* fix: ``rebuildall(verbose=True)`` no longer truncates the post-rebuild flush.
  ``verbose`` (a logging flag) was passed positionally to ``flush()``, whose
  first parameter is ``run_once`` — so ``verbose=True`` stopped the flush after
  a single pass and left dependency-cascade markers unprocessed.
* fix: ``denorm_flush_via_queue`` no longer crashes with
  ``TypeError: object of type 'EagerResult' has no len()``. It treated the
  return value of ``flush_via_queue`` (the chord callback's ``AsyncResult``) as
  a ``GroupResult``. The command now dispatches the task fire-and-forget and
  polls the ``DirtyInstance`` table until it drains, bounded by a new
  ``--timeout`` (and ``--poll-interval``). Multi-pass chord convergence means
  there is no single group to await.
* fix: ``denorm_queue`` daemon robustness — the reconnect backoff now **resets**
  to its base after a successful (re)connection (a flapping DB no longer
  accumulates an ever-growing wait); ``select()`` uses a finite keepalive
  timeout (so a silently dead connection is surfaced and shutdown is prompt)
  and a signal-interrupted ``select`` (``InterruptedError``) no longer crashes
  the daemon; the ``LISTEN`` cursor is closed promptly instead of leaking one
  per reconnect.
* feature: ``DenormMiddleware`` is now configurable via the
  ``DENORM_MIDDLEWARE_FLUSH`` setting (``"inline"`` (default) / ``"queue"`` /
  ``"off"``) and only flushes when ``DirtyInstance`` markers actually exist
  (a cheap ``exists()`` guard, correct for bulk/raw writes too). In ``"queue"``
  mode it dispatches ``flush_via_queue`` instead of blocking the request; in
  ``"off"`` mode it does nothing, deferring to the ``denorm_queue`` daemon.

1.12.1 (2026-06-13)
-------------------

* fix: triggers now resolve a model's ``django_content_type`` id with a
  ``(SELECT id FROM django_content_type WHERE app_label = ... AND model = ...)``
  subquery evaluated **at trigger-fire time**, instead of baking the id as an
  integer literal at trigger-build time. A baked literal is only correct while
  the content-type table is never renumbered after the triggers are installed;
  that assumption breaks under Django's ``TransactionTestCase`` teardown (which
  TRUNCATEs ``django_content_type`` and lets ``post_migrate`` recreate the rows
  with drifting ids) and, more rarely, after a restored dump that renumbered the
  table or a ``remove_stale_contenttypes`` + recreate without a trigger rebuild.
  When the literal went stale the marker insert referenced a content type that no
  longer existed and the FK ``denorm_dirtyinstance_content_type_id_...`` raised
  ``ForeignKeyViolation``. The lookup is a single probe of the
  ``(app_label, model)`` unique index on a tiny, fully cached table, so the
  per-fire cost is negligible. (The scalar pk is still used for the trigger name
  and ``WHEN`` clause, which cannot contain a subquery.)
* fix: ``drop_triggers()`` now matches the real trigger naming
  (``d_{aft,bef}_row_{ins,upd,del}_on_<table>``, plus the legacy ``denorm_%``
  prefix). The old ``LIKE 'denorm_%'`` matched none of the library's own
  triggers, so ``drop_triggers()`` / ``denorm_drop`` / the drop half of
  ``denorm_rebuild_triggers`` were silent no-ops. Because ``install_triggers``
  only ``CREATE OR REPLACE``-s same-named triggers, an upgrade that renames a
  trigger (e.g. the 1.11 → 1.12 self-trigger rename) would orphan the old one —
  and nothing could clean it. A drop+install rebuild now genuinely removes the
  whole set first. ``DROP TRIGGER IF EXISTS`` guards against races.

1.12.0 (2026-06-13)
-------------------

* perf: ``flush_via_queue`` now **self-converges** via a Celery chord.
  Each dispatch fans the current backlog out into ``flush_batch`` tasks
  and, when they finish, re-dispatches itself if any object was marked
  dirty during processing (cross-object cascades) — until the
  ``DirtyInstance`` table is empty. This mirrors the inline ``flush()``
  convergence loop, so the queue path no longer depends on a fresh
  ``LISTEN/NOTIFY`` to finish draining a cascade. Bounded by the new
  ``DENORM_MAX_QUEUE_PASSES`` setting (default ``100``), the queue
  analogue of ``DENORM_MAX_FLUSH_PASSES``.
* fix: ``flush_batch`` now **isolates per-pair failures**. If
  ``flush_single`` raises for one ``(content_type_id, object_id)`` pair
  (retry-exhausted serialization failure, or any non-transient error),
  the exception is logged at ``ERROR`` level with full traceback and the
  ``(ct, oid)`` pair, and iteration continues with the next pair.
  The task returns normally so the chord callback (``_flush_requeue``) is
  guaranteed to run — preventing a single bad object from stalling the
  entire convergence round. The failing object's ``DirtyInstance`` marker
  persists (``flush_single``'s transaction rolled back, so the marker was
  never deleted) and is retried automatically on the next round, bounded
  by ``DENORM_MAX_QUEUE_PASSES``.
* perf: marker INSERTs performed *during a flush* no longer emit a NOTIFY
  wake-up. ``flush_single`` sets a transaction-local GUC
  (``SET LOCAL denorm.flushing = 'on'``) and migration ``0019`` guards
  the notify trigger function with it, so only genuine writes
  (application saves, bulk updates, raw SQL) wake the ``denorm_queue``
  channel. This removes the wasted NOTIFY→no-op-flush churn under heavy
  cascades. Migration ``0019`` is reversible (restores the unconditional
  NOTIFY from ``0016``).
* **Dropped Django 4.2 support** (extended support ended April 2026).
  Minimum is now Django 5.2 LTS. CI tests Django 5.2 and 6.0.
* fix: ``flush_single`` now deletes claimed ``DirtyInstance`` markers at
  claim time. Previously, the unique-index dedup could silently swallow
  concurrent invalidation markers inserted between the claim and the
  delete, permanently losing them until the next full rebuild.
* fix: merged triggers now union their watched-column lists. When two
  triggers collided on a name, ``TriggerSet.append`` merged only the
  newcomer's actions and kept the existing trigger's watch-list, silently
  dropping any column watched solely by the newcomer; a change to that
  column would not fire the trigger, causing latent missed invalidation
  (stale data). The watch-lists are now unioned (harmless over-fire).
* feature: ``@depend_on_fields(*names)`` — declarative same-model
  dependencies for ``@denormalized`` functions. A declared function gets
  a targeted per-function database trigger that fires only when a
  declared column changes. ``@depend_on_fields()`` with no arguments
  means "reads no sibling columns" and emits no UPDATE self-trigger for
  that function (the INSERT marker and any ``@depend_on_related``
  triggers remain).
* Per-function triggers replace the old catch-all self-trigger: the
  library's triggers no longer emit ``func_name=NULL`` markers.
  Conservative (undeclared) functions get an any-watched-column trigger
  that excludes the function's own column. Consequence: bulk-writing a
  denormalized column directly is unsupported and does not mark anything
  dirty; write source columns instead or call ``mark_dirty()``.
* ``NULL`` contract: ``func_name=NULL`` means "recompute every
  denormalized field of this object" and takes precedence over
  field-level markers during flush. Only ``mark_dirty(*instances)`` and
  ``rebuildall``/``rebuild_instances_of`` produce NULL markers now.
* feature: ``denorm.mark_dirty(*instances)`` — explicitly marks whole
  objects dirty (NULL markers).
* feature: ``@denorm.denorm_always_dirty`` model class decorator — every
  user ``save()`` of the decorated model unconditionally inserts a
  ``func_name=NULL`` dirty marker, guaranteeing that all its denormalized
  fields are recomputed by the next ``flush()``.  ``flush()`` converges
  normally: it recomputes and clears the marker, and its own recompute save
  does not re-mark the object (a thread-local flush-in-progress guard).
  Intended for denorms whose value depends on inputs the trigger /
  ``@depend_on_related`` / ``@depend_on_fields`` system cannot express
  (external state, time-based values, complex cross-table reads).
  ``QuerySet.update()`` / ``bulk_create()`` / raw SQL bypass ``post_save`` and
  therefore bypass this decorator — use ``mark_dirty()`` explicitly for those
  paths.
* feature: ``DENORM_MAX_FLUSH_PASSES`` setting (default ``100``):
  ``flush()`` aborts with an error log naming the still-dirty models
  instead of looping forever when a denormalized function is
  non-deterministic.
* feature: Django system checks ``denorm.E001``, ``denorm.E002``,
  ``denorm.W001``, ``denorm.W002`` audit ``@depend_on_fields``
  declarations using an AST scanner. An incomplete declaration causes
  silent staleness, exactly like a missing ``@depend_on_related``.
  Silence individual checks via ``SILENCED_SYSTEM_CHECKS``. PK reads
  are ignored.
* Targeted flush saves now use ``update_fields`` — ``auto_now`` columns
  are no longer touched by targeted flushes.
  ``DENORM_DISABLE_AUTOTIME_DURING_FLUSH`` now only applies to
  whole-object (``func_name=NULL``) flushes.
* **Upgrade note**: run ``manage.py denorm_rebuild_triggers`` after
  upgrading — trigger SQL changed shape (per-function markers, ON CONFLICT,
  WHEN clause).
* fix: ``denorm_queue`` now drains ``pg_con.notifies`` after each
  ``poll()`` call. Previously, the list grew without bound under
  sustained write traffic, leaking memory in this long-running daemon
  (spec 1.1).
* fix: ``denorm_queue`` fires one ``flush_via_queue.delay()`` immediately
  after ``LISTEN`` succeeds, before entering the select loop. PostgreSQL
  does not queue NOTIFYs for disconnected listeners; dirty rows
  accumulated during a deploy or failover are now flushed without waiting
  for the next unrelated write. ``Singleton`` deduplicates if a flush is
  already queued (spec 1.2).
* fix: ``DENORM_SINGLETON_LOCK_EXPIRY`` setting (default ``600`` seconds)
  is now passed as ``lock_expiry`` to every ``celery-singleton``-based
  task. Without an expiry, a SIGKILLed worker left its Redis lock
  forever, permanently wedging enqueuing for the affected object (spec 1.3).
* fix: ``CountField`` / ``SumField`` ``pre_save`` no longer SELECTs the
  current counter and writes it back. It now writes ``col = col`` (a
  Django ``F()`` expression), resolved inside the ``UPDATE`` itself.
  A concurrent trigger increment between any read and the UPDATE can no
  longer be silently overwritten. **Contract note**: ``save()`` leaves
  the in-memory attribute untouched; call ``refresh_from_db()`` when the
  current value matters (spec 1.4).
* perf: dirty-marker ``INSERT`` statements now use
  ``ON CONFLICT DO NOTHING`` instead of a plpgsql ``EXCEPTION`` block.
  The ``EXCEPTION`` form opened a subtransaction on every execution
  (even when no conflict occurred), a known PostgreSQL scalability cliff
  under high write concurrency (spec 2.1). Re-run
  ``manage.py denorm_rebuild_triggers`` after upgrading.
* perf: UPDATE-trigger change-detection (``OLD.col IS DISTINCT FROM
  NEW.col``) now lives in the ``CREATE TRIGGER … WHEN (…)`` clause
  instead of an ``IF`` inside the trigger function. PostgreSQL evaluates
  ``WHEN`` before invoking plpgsql, so rows that touch no watched column
  skip the function entirely (spec 2.2). Re-run
  ``manage.py denorm_rebuild_triggers`` after upgrading.
* perf: ``flush_single`` now resolves ``ContentType`` via
  ``ContentType.objects.get_for_id()`` (process-level cache) instead of
  ``ContentType.objects.get(pk=…)``. A 100k-row flush previously issued
  100k identical queries (spec 2.3). ``flush()`` now iterates distinct
  pairs with ``.iterator(chunk_size=2000)`` (server-side cursor) instead
  of materialising the full list in memory (spec 2.4).
* perf: ``flush_via_queue`` now dispatches a ``celery.group`` of
  ``flush_batch`` tasks, each covering ``DENORM_QUEUE_CHUNK_SIZE``
  (default ``50``) ``(content_type_id, object_id)`` pairs. Previously
  one Celery task was created per pair — a 500k-row backlog produced
  500k broker messages. The legacy ``flush_single`` task is kept as a
  thin wrapper for one release so tasks already sitting in brokers during
  a rolling deploy continue to execute (spec 2.4).
* perf: marker-claim queries now use
  ``COALESCE(object_id, -1)`` to match the expression index created in
  migration 0017, making per-object marker lookups O(log N) instead of
  O(N) for content-type-dominated backlogs (spec 2.6). Three redundant
  indexes were removed (``func_name``, ``created_on``, and the automatic
  FK index on ``content_type``); migration ``0018`` handles this.
  Users who filter ``DirtyInstance`` themselves should review
  ``pg_stat_user_indexes`` and re-add needed indexes in their own apps.
* perf: ``flush_single`` now converges same-model denorm chains inside
  one transaction (spec 2.5) instead of requiring one outer ``flush()``
  pass per chain link — fewer transactions and locks on the hot path. For
  example, a two-link chain (``first_name`` → ``full_name`` →
  ``letterhead``) is now settled in a single ``flush_single`` call with
  two targeted ``update_fields`` saves instead of two separate
  transactions. New setting ``DENORM_MAX_CONVERGE_PASSES`` (default ``5``)
  caps the inner loop for non-deterministic functions; on hitting the cap,
  remaining markers fall back to the outer ``flush()`` loop (bounded by
  ``DENORM_MAX_FLUSH_PASSES``). No API change, no migration, no trigger
  SQL change (``denorm_rebuild_triggers`` is not needed for this fix).
* perf: ORM saves drop provably-redundant self-markers (plain-column
  same-model denorm fields) in a ``post_save`` handler, eliminating the
  redundant ``flush()`` re-save for the common compute-from-own-columns
  pattern.  Chain denorms (depending on another denorm field), related
  denorms (``@depend_on_related``), and bulk/raw writes are unaffected.
* ``denorm_flush_via_queue`` command now uses ``result.get(timeout=…)``
  instead of ``time.sleep(0.5)`` and times progress against the number
  of dispatched tasks rather than raw ``DirtyInstance`` rows. A Celery
  result backend is required (spec 3.2).
* **Testing**: the test suite now runs the Celery queue against a real Redis
  broker and result backend (``testcontainers``, ``RedisContainer``). A handful
  of end-to-end tests use a real in-process Celery worker (``eager OFF``) to
  exercise the genuine broker serialization → worker round-trip → ``flush_batch``
  group fan-out → ``flush_single`` drain path. The rest use ``task_always_eager``
  for speed. Singleton lock-backend dedup is tested with direct Redis lock
  assertions (deterministic, no race). Contributors now need Docker for the full
  test suite (already required for Postgres). No runtime or API change.
* Dead code removed: Django < 4.2 compatibility branches
  (``add_lazy_relation``, Django 1.8–1.10 try/excepts, version-guarded
  middleware wrapper); unused ``DirtyInstance`` helpers
  (``DEFAULT_TIMEOUT``, ``WEEK_AGO``, ``find_similar``,
  ``delete_similar``, ``delete_this_and_similar``); unused
  ``denorm_queue_name`` variable in ``db/triggers.py``; unused
  ``Denorm.update()`` (spec 3.3).
* feature: ``DENORM_ALWAYS_EAGER`` setting (default ``False``) — test-only
  synchronous flush after every ``post_save`` / ``post_delete`` /
  ``m2m_changed`` signal. Mirrors Celery's ``task_always_eager``: denorm
  fields on dependent objects and same-model chains settle immediately
  without a manual ``denorm.flush()`` call or a Celery worker. Enable
  per-test with ``@override_settings(DENORM_ALWAYS_EAGER=True)``. **Not
  for production** — reintroduces synchronous coupling. Bulk paths
  (``QuerySet.update()``, ``bulk_create()``, ``bulk_update()``,
  ``mark_dirty()``) fire no per-row signals and are not auto-flushed; call
  ``denorm.flush()`` explicitly after bulk writes.

1.11.1
------

* fix: ``denorm.tasks.flush_single`` is now keyed by the logical
  ``(content_type_id, object_id)`` pair instead of a representative
  ``DirtyInstance`` pk. Previously, if the captured marker was deleted
  by a concurrent flush path between ``flush_via_queue`` enqueue and
  task execution, and a fresh marker for the same pair was inserted in
  the meantime, the queued task aborted on ``DoesNotExist`` and the new
  marker was orphaned until the next flush cycle.
* fix: ``denorm/templates/denorm/dirty_instances_count.html`` is now
  included in the built wheel via ``[tool.setuptools.package-data]``.
  Without it, ``dirty_instances_count`` raised ``TemplateDoesNotExist``
  for users installing from PyPI.
* CI: ``tests/test_deadlocks.py`` (deadlock and race-regression suite)
  is now executed by ``tox`` alongside the existing ``test_app`` suite.

1.11.0
------

* Fix race conditions and deadlock handling in the flush pipeline
  (retry on serialization failures, deletion-by-pk after
  ``select_for_update``, deduplicated subtask fan-out, no global
  ``Field.auto_now`` mutation during flush).
* Add ``dirty_instances_count`` view with a configurable access policy
  (``DENORM_DIRTY_INSTANCES_VIEW_ACCESS`` = ``staff`` / ``authenticated``
  / ``public``).

1.10.2
------

* Django 6.0 compatibility: ``TriggerFilterQuery.JoinField`` implements
  ``get_joining_fields()`` alongside the legacy ``get_joining_columns()``.
  Removes ``RemovedInDjango60Warning: The usage of get_joining_columns()
  in Join is deprecated. Implement get_joining_fields() instead``.
* ``Field.pre_save()`` calls are now idempotent on Django 6.0.

1.10.1
------

* packaging migrated from ``setup.py``/``setup.cfg`` to ``pyproject.toml``
  with ``uv``,
* CI updated for Python 3.10-3.13 and Django 4.2/5.2 LTS,
* GitHub Actions upgraded to Node.js 24-compatible versions; CI badge
  link added to README,
* README refreshed with current Python/Django versions, license info
  and a support matrix,
* pre-commit: added ``detect-private-key`` hook, ``pyupgrade`` bumped
  to ``--py310-plus``,
* ``.gitignore`` expanded for modern Python development,
* Django 5 compatibility: removed ``NullBooleanField`` and deprecated
  settings.

1.10.0
------

* fixes for a race condition in queue processing.

1.9.0
-----

* switched from ``SKIP LOCKED`` to ``NOWAIT`` when acquiring row locks
  to fail fast instead of silently skipping rows.

1.8.0
-----

* optimizations around the ``NOWAIT`` locking path.

1.7.0
-----

* narrowed the scope of row-level locks during queue flushing to
  reduce contention.

1.6.0
-----

* another attempt at fixing the recurring deadlock/lock contention
  issues when multiple ``denorm_queue`` workers run in parallel.

1.5.0
-----

* skip similar, already-locked items when flushing to avoid deadlocks.

1.4.1
-----

* further fixes for skipping already-locked similar items to avoid
  deadlocks.

1.4.0
-----

* integration with ``celery-singleton`` to prevent duplicate Celery
  tasks from running simultaneously.

1.3.0
-----

* switched back to non-parametrized ``NOTIFY``,
* triggers emit ``NOTIFY`` only on ``AFTER STATEMENT`` to wake up the
  flusher rather than on every row change.

1.2.2
-----

* Celery tasks default to ignored results,
* queue flushing pushed to the backend (Celery worker),
* small fixes: added type to a parameter, corrected ``__str__``.

1.2.1
-----

* new management command to flush via Celery queues,
* pop all pending NOTIFY messages before flushing.

1.2.0
-----

* queue code refactored to use Celery.

1.1.5
-----

* attempt to fix recurring deadlock issues.

1.1.4
-----

* attempt to fix an issue with handling deletes.

1.1.3
-----

* further attempts at fixing deadlocks.

1.1.2
-----

* added ``skip_locked=True`` to avoid deadlocks,
* Django 4.2 and 5.0 support,
* tested against Python 3.11 and 3.12,
* test app migrations and general housekeeping,
* test fixes.

1.1.1
-----

* updated for Django 4.2 (work in progress),
* enabled more Django configurations in CI.

1.1.0
-----

* fixed ``NullBooleanField`` handling,
* enabled parallel ``tox`` testing,
* migrated CI from Travis to GitHub Actions,
* temporarily disabled Django 4.0,
* assorted build and link fixes.

1.0.0
-----

* try to avoid deadlocks during queue processing,
* first stable 1.x release.

0.5.5
-----

* changes to reduce the chance of multiple ``denorm_queue`` processes trying
  to denormalize the same object

0.5.4
-----

* don't wait for content_object when flushing queue, so we won't get deadlocks and
  Django exceptions

0.5.3
-----

* select_for_update also for the updated object, so we won't get deadlocks

0.5.2
-----

* include missing ``conf`` package.

0.5.1
-----

* optimized denorms.rebuildall, using bulk_create,
* denorm_rebuild command gets 2 new command-line options, model_name and no_flush,
* ability to disable auto_now_add and auto_now fields during denorm flush, using
  settings -- DENORM_DISABLE_AUTOTIME_DURING_FLUSH and field names
  DENORM_AUTOTIME_FIELD_NAMES,
* denorms.flush works in batches now.

0.5.0
-----

* first release of django-denorm-iplweb,
* based on the high-quality code of the original django-denorm_
* supported versions: Python 3.8, 3.9, Django 3.0, 3.1, 3.2,
* dropped support for MySQL,
* dropped support for SQLite,
* denorm_daemon becomes denorm_queue:
  - removed daemonzation code,
  - documented need to use supervisord or similar if background process needed,
  - used LISTEN/NOTIFY mechanisms from PostgreSQL,
* removed six dependency and __unicode__,
* added pre-commit hooks for autopep, flake8,
* added bumpver configuration,
* automatic trigger installation after post_migrate,
* documentation updated,
* post_migration signal causes trigger rebuild,
* ``rebuild_triggers`` command to rebuild triggers,
* deprecated command ``denormalize`` removed,
* field names given as a parameter to ``skip`` or ``denorm_always_skip`` are checked if they exist,
* triggers and functions names, generated for ``@depend_on_related`` include function (attribute) name,
* DirtyInstance includes func_name, which is a function name to rebuild only this single parameter
* ability to run multiple ``denorm_queue`` commands, which (thanks to the magic of row locking) should
  automatically process queue in a paralell manner.


.. _django-denorm: https://github.com/django-denorm/django-denorm
