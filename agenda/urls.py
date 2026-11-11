from django.urls import path
from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("api/state/", views.api_state, name="api_state"),
    path("api/plans/create/", views.api_create_plan, name="api_create_plan"),
    path("api/plans/reschedule/", views.api_reschedule_plan, name="api_reschedule_plan"),
    path("api/plans/complete/", views.api_complete_plan, name="api_complete_plan"),
    path("api/memories/create/", views.api_create_memory, name="api_create_memory"),
    path("manifest.webmanifest", views.manifest_view, name="manifest"),
    path("sw.js", views.service_worker_view, name="service_worker"),
    path("favicon.svg", views.favicon_view, name="favicon"),
    path("descargar-apk/", views.download_apk_view, name="download_apk"),
    path("healthz/", views.healthz_view, name="healthz"),
]
