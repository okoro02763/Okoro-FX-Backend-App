"""URL routes for the monitoring app (mounted under ``/api/monitoring/``)."""

from django.urls import path

from . import views

urlpatterns = [
    path('test-alert/', views.test_alert, name='monitoring-test-alert'),
    path('client-error/', views.client_error, name='monitoring-client-error'),
]
