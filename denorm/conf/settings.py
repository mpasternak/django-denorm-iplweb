from django.conf import settings

DENORM_DISABLE_AUTOTIME_DURING_FLUSH = getattr(
    settings, "DENORM_DISABLE_AUTOTIME_DURING_FLUSH", False
)

DENORM_AUTOTIME_FIELD_NAMES = getattr(settings, "DENORM_AUTOTIME_FIELD_NAMES", [])

DENORM_BATCH_SIZE = getattr(settings, "DENORM_BATCH_SIZE", 5000)
