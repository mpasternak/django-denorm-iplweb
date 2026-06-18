from django.conf import settings

DENORM_DISABLE_AUTOTIME_DURING_FLUSH = getattr(
    settings, "DENORM_DISABLE_AUTOTIME_DURING_FLUSH", False
)

DENORM_AUTOTIME_FIELD_NAMES = getattr(settings, "DENORM_AUTOTIME_FIELD_NAMES", [])

DENORM_BATCH_SIZE = getattr(settings, "DENORM_BATCH_SIZE", 5000)

# Safety valve for denorm.flush(): maximum number of passes over the
# DirtyInstance table before aborting with an error log. Prevents an
# infinite flush loop when a denormalized function is non-deterministic
# (returns a different value on every recompute).
DENORM_MAX_FLUSH_PASSES = getattr(settings, "DENORM_MAX_FLUSH_PASSES", 100)

# Maximum number of in-transaction convergence iterations flush_single runs to
# settle a same-model denorm chain (e.g. full_name -> letterhead). Caps
# non-deterministic denorm functions; markers beyond the cap stay in the table
# and are handled by flush()'s outer loop (bounded by DENORM_MAX_FLUSH_PASSES).
DENORM_MAX_CONVERGE_PASSES = getattr(settings, "DENORM_MAX_CONVERGE_PASSES", 5)

# Access policy for denorm.views.dirty_instances_count.
# One of: "staff" (default), "authenticated", "public".
DENORM_DIRTY_INSTANCES_VIEW_ACCESS = getattr(
    settings, "DENORM_DIRTY_INSTANCES_VIEW_ACCESS", "staff"
)

# Redis lock TTL (seconds) for celery-singleton tasks. Without an expiry,
# a SIGKILLed worker leaves its lock forever and the affected object can
# never be enqueued again.
DENORM_SINGLETON_LOCK_EXPIRY = getattr(settings, "DENORM_SINGLETON_LOCK_EXPIRY", 600)

# Number of (content_type_id, object_id) pairs handled by one celery task.
DENORM_QUEUE_CHUNK_SIZE = getattr(settings, "DENORM_QUEUE_CHUNK_SIZE", 50)

# Queue analogue of DENORM_MAX_FLUSH_PASSES: maximum number of chord
# re-dispatch passes flush_via_queue runs to self-converge (drain cross-object
# cascade markers created during processing) before aborting with an error log.
# Bounds a non-deterministic denorm so the chord cannot re-dispatch forever.
DENORM_MAX_QUEUE_PASSES = getattr(settings, "DENORM_MAX_QUEUE_PASSES", 100)

# Test-only: when True, denorm flushes synchronously after each signalled
# write (post_save/post_delete/m2m_changed), so denorm fields — including those
# on dependent objects and same-model chains — are correct immediately without
# a manual denorm.flush(). Mirrors Celery's task_always_eager. NOT for
# production (reintroduces synchronous coupling); default off keeps the
# deferred model. See denorm/eager.py.
DENORM_ALWAYS_EAGER = getattr(settings, "DENORM_ALWAYS_EAGER", False)

# Controls how denorm.middleware.DenormMiddleware flushes after a request.
# One of:
#   "inline" (default) — flush synchronously during the response cycle.
#   "queue"            — dispatch flush_via_queue to Celery; don't block the request.
#   "off"              — never flush in the middleware; rely on the denorm_queue
#                        daemon or a manual/cron flush instead.
# In "inline"/"queue" modes the flush is skipped unless DirtyInstance markers
# exist. The middleware reads this LIVE from django settings (so override_settings
# works); this entry documents the default.
DENORM_MIDDLEWARE_FLUSH = getattr(settings, "DENORM_MIDDLEWARE_FLUSH", "inline")
