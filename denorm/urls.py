from django.urls import path

from denorm import views

app_name = "denorm"

urlpatterns = [
    path(
        "dirty-instances/",
        views.dirty_instances_count,
        name="dirty_instances_count",
    ),
]
