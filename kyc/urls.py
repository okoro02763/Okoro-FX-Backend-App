from django.urls import re_path

from . import views


def api_route(route, view, name):
    """Answer with or without a trailing slash, like the forex app does."""
    return re_path(rf'^{route}/?$', view, name=name)


urlpatterns = [
    api_route('', views.kyc_status, name='kyc-status'),
    api_route('details', views.save_details, name='kyc-details'),
    api_route('documents', views.upload_document, name='kyc-documents'),
    api_route(
        r'documents/(?P<document_id>[0-9]+)',
        views.delete_document,
        name='kyc-document-delete',
    ),
    api_route('submit', views.submit, name='kyc-submit'),
    # Short-lived signed file URLs (staff-only in practice; the token is
    # minted by the admin).
    re_path(r'^files/(?P<token>[^/]+)/?$', views.download_document, name='kyc-file'),
]
