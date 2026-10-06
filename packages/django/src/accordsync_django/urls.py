"""`path("", include("accordsync_django.urls"))` serves /v1/push, /v1/pull and /health."""

from django.urls import path

from . import views

app_name = "accordsync"
urlpatterns = [
    path("v1/push", views.push, name="push"),
    path("v1/pull", views.pull, name="pull"),
    path("health", views.health, name="health"),
]
