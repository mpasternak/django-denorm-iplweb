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

# Access policy for denorm.views.dirty_instances_count.
# One of: "staff" (default), "authenticated", "public".
DENORM_DIRTY_INSTANCES_VIEW_ACCESS = getattr(
    settings, "DENORM_DIRTY_INSTANCES_VIEW_ACCESS", "staff"
)

# Redis lock TTL (seconds) for celery-singleton tasks. Without an expiry,
# a SIGKILLed worker leaves its lock forever and the affected object can
# never be enqueued again.
DENORM_SINGLETON_LOCK_EXPIRY = getattr(settings, "DENORM_SINGLETON_LOCK_EXPIRY", 600)
