from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ImproperlyConfigured
from django.db.models import Count
from django.shortcuts import render

from denorm.conf import settings as denorm_settings
from denorm.models import DirtyInstance

ACCESS_DECORATORS = {
    "staff": staff_member_required,
    "authenticated": login_required,
    "public": lambda view: view,
}


def _apply_access_decorator(view):
    policy = denorm_settings.DENORM_DIRTY_INSTANCES_VIEW_ACCESS
    try:
        decorator = ACCESS_DECORATORS[policy]
    except KeyError:
        raise ImproperlyConfigured(
            f"DENORM_DIRTY_INSTANCES_VIEW_ACCESS must be one of "
            f"{sorted(ACCESS_DECORATORS)}, got {policy!r}."
        )
    return decorator(view)


def _dirty_instances_count_view(request):
    counts = (
        DirtyInstance.objects.values("content_type")
        .annotate(count=Count("id"))
        .order_by("-count")
    )

    ct_map = ContentType.objects.in_bulk([item["content_type"] for item in counts])

    rows = []
    total = 0
    for item in counts:
        ct = ct_map.get(item["content_type"])
        label = (
            f"{ct.app_label}.{ct.model}"
            if ct
            else f"<deleted content type #{item['content_type']}>"
        )
        rows.append({"model": label, "count": item["count"]})
        total += item["count"]

    return render(
        request,
        "denorm/dirty_instances_count.html",
        {"rows": rows, "total": total},
    )


dirty_instances_count = _apply_access_decorator(_dirty_instances_count_view)
